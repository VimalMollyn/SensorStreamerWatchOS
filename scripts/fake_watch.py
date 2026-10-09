#!/usr/bin/env python3
"""Send a synthetic SensorStreamer stream, to test the receiver and viewer
without the watch.

    uv run python scripts/fake_watch.py                          # to 127.0.0.1:5005
    uv run python scripts/fake_watch.py --channels 3 --fused --host 192.168.1.5

Audio is a 440 Hz tone over noise with a click once a second; the same click
is a spike on the accelerometer's z axis, so the two should line up on
screen. Same datagrams, same pacing, same batching as the watch.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sensorstreamer import wire  # noqa: E402


def imu_payload(times, t0, values) -> bytes:
    records = np.column_stack(((times - t0).astype(np.float32), values.astype(np.float32)))
    return np.ascontiguousarray(records, dtype="<f4").tobytes()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send a synthetic SensorStreamer stream.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--channels", type=int, default=1, help="audio channels (1..3)")
    parser.add_argument("--rate", type=int, default=48000)
    parser.add_argument("--imu-rate", type=int, default=100)
    parser.add_argument("--fused", action="store_true", help="also send device motion")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = parser.parse_args(argv)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = (args.host, args.port)
    fs = args.rate
    ch = max(1, min(3, args.channels))
    per = min(480, 1408 // (2 * ch) // 16 * 16)
    imu_rate = args.imu_rate
    imu_per = max(1, imu_rate // 20)
    gains = np.array([1.0, 0.5, 0.25][:ch])
    rng = np.random.default_rng(0)

    wall0 = time.time()
    mono0 = time.monotonic()
    pcm_index = 0
    imu_index = 0
    text_index = 0
    next_hb = 0.0

    def send_text(text):
        nonlocal text_index
        payload = text.encode()
        sock.sendto(wire.pack("text", 0, len(payload), text_index, 0.0, time.time(), payload), addr)
        text_index += 1

    def send_imu(kind, times, values):
        sock.sendto(wire.pack(kind, values.shape[1], len(times), imu_index, imu_rate,
                              times[0], imu_payload(times, times[0], values)), addr)

    print(f"sending to {addr[0]}:{addr[1]}: pcm {ch} ch at {fs} Hz ({per} samples/datagram), "
          f"imu {imu_rate} Hz ({imu_per} records/datagram){', fused' if args.fused else ''}")
    send_text(f"start: fake watch, pcm {ch} ch at {fs} Hz, imu {imu_rate} Hz")

    while True:
        elapsed = time.monotonic() - mono0
        if args.seconds is not None and elapsed >= args.seconds:
            break
        while (pcm_index + per) / fs <= elapsed:
            t = np.arange(pcm_index, pcm_index + per) / fs
            phase = t % 1.0
            x = (0.1 * np.sin(2 * np.pi * 440 * t)
                 + 0.01 * rng.standard_normal(per)
                 + 0.6 * np.exp(-phase / 0.002) * (phase < 0.02))
            samples = np.clip(x[:, None] * gains[None, :] * 32767, -32768, 32767)
            payload = np.ascontiguousarray(samples, dtype="<i2").tobytes()
            sock.sendto(wire.pack("pcm", ch, per, pcm_index, fs, wall0 + pcm_index / fs, payload), addr)
            pcm_index += per
        while (imu_index + imu_per) / imu_rate <= elapsed:
            t = np.arange(imu_index, imu_index + imu_per) / imu_rate
            phase = t % 1.0
            spike = 2.0 * np.exp(-phase / 0.02) * (phase < 0.1)
            acc = np.column_stack((0.3 * np.sin(2 * np.pi * 0.5 * t),
                                   0.2 * np.cos(2 * np.pi * 0.5 * t),
                                   -1.0 + spike))
            gyr = np.column_stack((0.5 * np.sin(2 * np.pi * 0.7 * t),
                                   0.3 * np.sin(2 * np.pi * 1.1 * t),
                                   0.1 * np.cos(2 * np.pi * 0.3 * t)))
            times = wall0 + t
            send_imu("acc", times, acc)
            send_imu("gyr", times, gyr)
            if args.fused:
                theta = 2 * np.pi * 0.1 * t
                motion = np.column_stack((np.cos(theta / 2), np.zeros_like(t), np.zeros_like(t),
                                          np.sin(theta / 2),
                                          gyr,
                                          np.zeros_like(t), np.zeros_like(t), spike,
                                          np.zeros_like(t), np.zeros_like(t), -np.ones_like(t)))
                send_imu("motion", times, motion)
            imu_index += imu_per
        if elapsed >= next_hb:
            send_text(f"hb pps=0 fake=1 pcm={pcm_index} imu={imu_index}")
            next_hb += 2.0
        time.sleep(0.002)
    send_text("stop")
    print(f"sent {pcm_index} pcm samples, {imu_index} imu records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
