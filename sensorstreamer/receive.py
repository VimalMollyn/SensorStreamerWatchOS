#!/usr/bin/env python3
"""Receive the SensorStreamer stream from the watch: audio, IMU, status.

The watch app sends the binary UDP datagrams described in wire.py: Int16
audio (48 kHz, one or all microphone channels), 100 Hz accelerometer,
gyroscope, magnetometer if the watch exposes one, optionally the fused
device motion, and text events. Everything is stamped on the watch's one
clock, so the streams line up.

    sensorstreamer-receive                       # listen on 0.0.0.0:5005, print stats
    sensorstreamer-receive --plot                # spectrogram and IMU traces, live
    sensorstreamer-receive --plot --out recordings
    sensorstreamer-receive --echo                # print every IMU record

With --out, each run writes a timestamped folder holding audio.wav (Int16,
the channels as sent, a lost datagram left as silence of the right length),
audio.json (rate, channels, the wall time of sample 0), one CSV per IMU
stream (t, then the values named in wire.FIELDS) and events.csv. Ctrl-C
stops. Standard library plus numpy; --plot needs pyglet (see live_plot.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import wire

DEFAULT_PORT = 5005
RECV_BUFSIZE = 65535
RECV_BUFFER_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------- recording

def wav_header(sample_rate: int, data_bytes: int, channels: int, bits=16) -> bytes:
    block_align = channels * bits // 8
    return b"".join([
        b"RIFF", struct.pack("<I", 36 + data_bytes), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                             sample_rate * block_align, block_align, bits),
        b"data", struct.pack("<I", data_bytes),
    ])


class WavWriter:
    """16-bit WAV written at exact sample offsets, so a lost datagram is a
    correctly sized hole of silence rather than a shortened file."""

    HEADER_BYTES = 44

    def __init__(self, path: Path, sample_rate: int, channels: int, base_index: int):
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.base = base_index
        self.handle = path.open("wb")
        self.handle.write(bytes(self.HEADER_BYTES))
        self.end = 0
        self.received = 0

    def write(self, first_index: int, pcm: bytes):
        offset = (first_index - self.base) * 2 * self.channels
        if offset < 0:
            return
        self.handle.seek(self.HEADER_BYTES + offset)
        self.handle.write(pcm)
        self.end = max(self.end, offset + len(pcm))
        self.received += len(pcm) // (2 * self.channels)

    def close(self):
        self.handle.seek(0)
        self.handle.write(wav_header(self.sample_rate, self.end, self.channels))
        self.handle.close()
        frames = self.end // (2 * self.channels)
        missing = frames - self.received
        note = f", {missing} samples lost" if missing else ""
        print(f"  wrote {self.path.name}: {frames / self.sample_rate:.1f}s, "
              f"{self.channels} ch at {self.sample_rate} Hz{note}")


class RecordingSink:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.wav = None
        self.imu = {}
        self._events = (self.directory / "events.csv").open("w", newline="")
        self._csv = csv.writer(self._events)
        self._csv.writerow(["host_time", "watch_time", "text"])
        print(f"  recording to {self.directory}")

    def pcm(self, packet: wire.Packet):
        if self.wav is None:
            self.wav = WavWriter(self.directory / "audio.wav", int(packet.rate),
                                 packet.channels, packet.index)
            (self.directory / "audio.json").write_text(json.dumps({
                "rate": packet.rate, "channels": packet.channels,
                "base_index": packet.index, "t0": packet.t0,
            }, indent=1))
            print(f"  writing {self.wav.path} ({packet.channels} ch at {packet.rate:.0f} Hz)")
        self.wav.write(packet.index, packet.payload)

    def imu_records(self, kind: str, times: np.ndarray, values: np.ndarray):
        writer = self.imu.get(kind)
        if writer is None:
            handle = (self.directory / f"{kind}.csv").open("w", newline="")
            writer = (handle, csv.writer(handle))
            writer[1].writerow(["t"] + wire.FIELDS.get(kind, [f"v{i}" for i in range(values.shape[1])]))
            self.imu[kind] = writer
        for t, row in zip(times, values):
            writer[1].writerow([f"{t:.6f}"] + [f"{v:.6g}" for v in row])

    def event(self, host_time: float, watch_time: float, text: str):
        self._csv.writerow([f"{host_time:.6f}", f"{watch_time:.6f}", text])

    def flush(self):
        self._events.flush()
        for handle, _ in self.imu.values():
            handle.flush()
        if self.wav:
            self.wav.handle.flush()

    def close(self):
        self._events.close()
        for kind, (handle, _) in self.imu.items():
            handle.close()
            print(f"  wrote {kind}.csv")
        if self.wav:
            self.wav.close()


# ---------------------------------------------------------------- receiver

class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.packets = {}
        self.bytes = {}
        self.samples = {}
        self.lost = {}
        self.next_index = {}
        self.rate = {}
        self.bad = 0
        self.last_hb = ""
        self.sender = None

    def packet(self, p: wire.Packet, nbytes: int):
        with self.lock:
            self.packets[p.kind] = self.packets.get(p.kind, 0) + 1
            self.bytes[p.kind] = self.bytes.get(p.kind, 0) + nbytes
            self.samples[p.kind] = self.samples.get(p.kind, 0) + p.count
            self.rate[p.kind] = p.rate
            if p.kind == "text":
                return
            expected = self.next_index.get(p.kind)
            if expected is not None and p.index > expected:
                self.lost[p.kind] = self.lost.get(p.kind, 0) + (p.index - expected)
            if expected is None or p.index >= expected or p.index < expected - 10_000_000:
                self.next_index[p.kind] = p.index + p.count

    def snapshot(self):
        with self.lock:
            s = (dict(self.packets), dict(self.bytes), dict(self.samples), dict(self.lost),
                 dict(self.rate), self.bad, self.last_hb, self.sender)
            self.packets.clear()
            self.bytes.clear()
            self.samples.clear()
            return s


def lan_addresses():
    """This Mac's IPv4 addresses, for typing into the watch."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
    except socket.gaierror:
        pass
    return found


def receive_loop(sock, args, stats: Stats, sink, plot, stop: threading.Event):
    last_report = time.time()
    last_flush = last_report
    while not stop.is_set():
        try:
            raw, addr = sock.recvfrom(RECV_BUFSIZE)
        except socket.timeout:
            raw = None
        except OSError:
            break
        now = time.time()
        if raw is not None:
            stats.sender = addr
            try:
                p = wire.parse(raw)
            except wire.ParseError as exc:
                stats.bad += 1
                if args.echo:
                    print(f"  bad datagram from {addr}: {exc}")
                p = None
            if p is None:
                pass
            elif p.kind == "text":
                stats.packet(p, len(raw))
                text = wire.text(p)
                if text.startswith("hb "):
                    stats.last_hb = text[3:]
                else:
                    print(f"  [{time.strftime('%H:%M:%S')}] watch: {text}")
                if sink:
                    sink.event(now, p.t0, text)
            elif p.kind == "pcm":
                stats.packet(p, len(raw))
                if sink:
                    sink.pcm(p)
                if plot:
                    samples = wire.pcm_samples(p)
                    for channel in range(p.channels):
                        plot.push_audio("watch", channel, now, p.t0, p.index, p.rate,
                                        samples[:, channel])
            else:
                stats.packet(p, len(raw))
                times, values = wire.imu_records(p)
                if sink:
                    sink.imu_records(p.kind, times, values)
                if plot:
                    plot.push_imu("watch", p.kind, now, times, values)
                if args.echo:
                    print(f"  {p.kind} #{p.index + p.count - 1} t={times[-1]:.3f} "
                          + " ".join(f"{v:+.3f}" for v in values[-1]))
        if now - last_report >= args.stats_every:
            packets, nbytes, samples, lost, rate, bad, hb, sender = stats.snapshot()
            dt = now - last_report
            last_report = now
            parts = []
            for kind in ("pcm",) + wire.IMU_KINDS:
                if kind not in packets:
                    continue
                if kind == "pcm":
                    parts.append(f"pcm {packets[kind] / dt:4.0f} pkt/s {nbytes[kind] / dt / 1000:6.1f} kB/s "
                                 f"{samples[kind] / dt / 1000:5.1f} kS/s")
                else:
                    parts.append(f"{kind} {samples[kind] / dt:4.0f} Hz")
            total_lost = sum(lost.values())
            if total_lost:
                parts.append(f"{total_lost} lost")
            if bad:
                parts.append(f"{bad} bad")
            if parts:
                print("  " + "  ".join(parts) + (f"   from {sender[0]}" if sender else ""))
                if hb:
                    print("    hb " + hb)
            elif not plot:
                print("  waiting for datagrams" + (f" (last sender {sender[0]})" if sender else ""))
        if sink and now - last_flush >= 5.0:
            sink.flush()
            last_flush = now


def make_plot(args):
    try:
        from .live_plot import LivePlot
    except ImportError as exc:
        print(f"error: --plot needs pyglet and numpy ({exc})", file=sys.stderr)
        print("       uv add pyglet", file=sys.stderr)
        return None
    db_range = None
    if args.db:
        lo, hi = (float(v) for v in args.db.split(","))
        db_range = (lo, hi)
    hidden = [h.strip() for h in args.hide.split(",") if h.strip()] if args.hide else []
    return LivePlot(window_seconds=args.window,
                    caption=f"SensorStreamer live · UDP {args.host}:{args.port}",
                    spec_fft=args.fft, spec_hop=args.hop, db_range=db_range, hidden=hidden,
                    snapshot=args.snapshot, exit_after=args.exit_after)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Receive the SensorStreamer stream from the watch.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out", type=Path, default=None,
                        help="record into a timestamped folder under this directory")
    parser.add_argument("--echo", action="store_true", help="print every IMU record")
    parser.add_argument("--plot", action="store_true", help="live spectrogram and traces (pyglet)")
    parser.add_argument("--window", type=float, default=5.0, help="seconds shown (default 5)")
    parser.add_argument("--fft", type=int, default=512, help="spectrogram FFT size")
    parser.add_argument("--hop", type=int, default=128)
    parser.add_argument("--db", default=None, help="pin the spectrogram's dB range, e.g. -90,-20")
    parser.add_argument("--hide", default=None,
                        help="panels to leave out, comma-separated, e.g. 'motion gravity,mag'")
    parser.add_argument("--stats-every", type=float, default=2.0)
    parser.add_argument("--exit-after", type=float, default=None,
                        help="with --plot: close the window after this many seconds")
    parser.add_argument("--snapshot", type=Path, default=None,
                        help="with --plot and --exit-after: save the window as this PNG on exit")
    args = parser.parse_args(argv)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
    except OSError:
        pass
    sock.bind((args.host, args.port))
    sock.settimeout(0.5)

    addresses = lan_addresses()
    print(f"listening on {args.host}:{args.port}")
    if addresses:
        print("this Mac is at " + ", ".join(addresses) + " -- type one into the watch's settings page")

    sink = None
    if args.out:
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        sink = RecordingSink(args.out / stamp)

    plot = make_plot(args) if args.plot else None
    if args.plot and plot is None:
        return 2

    stats = Stats()
    stop = threading.Event()
    worker = threading.Thread(target=receive_loop, args=(sock, args, stats, sink, plot, stop), daemon=True)
    worker.start()
    try:
        if plot:
            plot.run()          # the GL window owns the main thread on macOS
        else:
            while worker.is_alive():
                worker.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(2.0)
        sock.close()
        if sink:
            sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
