"""The datagram format, mirrored from SensorStreamerWatch/SensorStreamer/Wire.swift.

Every datagram is a 24-byte little-endian header and a payload:

    0   4   magic   "SSW1"
    4   1   kind    0 text, 1 pcm, 2 acc, 3 gyr, 4 mag, 5 motion
    5   1   ch      values per sample: pcm channels, or floats per IMU record
    6   2   count   samples (pcm), records (IMU) or bytes (text) that follow
    8   4   index   pcm: index of the first sample since start; IMU: of the
                    first record; text: a sequence number. A gap is a loss.
    12  4   rate    Float32, nominal samples per second
    16  8   t0      Float64, the watch's wall clock (unix s) at the first sample
    24  ... payload

pcm      count x ch Int16, interleaved; sample i is at t0 + i / rate.
IMU      count records of Float32 x (1 + ch): [t - t0, v0, v1, ...].
text     UTF-8; "hb ..." is the heartbeat, anything else an event.

Audio and motion are stamped on the same clock on the watch.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

MAGIC = b"SSW1"
HEADER = struct.Struct("<4sBBHIfd")
HEADER_SIZE = HEADER.size           # 24
KIND_NAMES = {0: "text", 1: "pcm", 2: "acc", 3: "gyr", 4: "mag", 5: "motion"}
KIND_IDS = {name: kind for kind, name in KIND_NAMES.items()}
IMU_KINDS = ("acc", "gyr", "mag", "motion")

FIELDS = {
    "acc": ["x", "y", "z"],                 # g
    "gyr": ["x", "y", "z"],                 # rad/s
    "mag": ["x", "y", "z"],                 # microtesla
    "motion": ["qw", "qx", "qy", "qz",      # attitude quaternion
               "rx", "ry", "rz",            # rotation rate, bias-corrected, rad/s
               "ux", "uy", "uz",            # user acceleration, g
               "gx", "gy", "gz"],           # gravity, g
}

# The viewer splits device motion into these panels: (tag, first, last, names).
MOTION_GROUPS = (
    ("motion quat", 0, 4, ["w", "x", "y", "z"]),
    ("motion rotrate", 4, 7, ["x", "y", "z"]),
    ("motion useracc", 7, 10, ["x", "y", "z"]),
    ("motion gravity", 10, 13, ["x", "y", "z"]),
)


class ParseError(Exception):
    pass


@dataclass
class Packet:
    kind: str
    channels: int
    count: int
    index: int
    rate: float
    t0: float
    payload: bytes


def parse(raw: bytes) -> Packet:
    if len(raw) < HEADER_SIZE:
        raise ParseError(f"{len(raw)} bytes is shorter than a header")
    magic, kind_id, channels, count, index, rate, t0 = HEADER.unpack_from(raw)
    if magic != MAGIC:
        raise ParseError(f"bad magic {magic!r}")
    kind = KIND_NAMES.get(kind_id)
    if kind is None:
        raise ParseError(f"unknown kind {kind_id}")
    payload = raw[HEADER_SIZE:]
    if kind == "text":
        expected = count
    elif kind == "pcm":
        expected = count * channels * 2
    else:
        expected = count * (1 + channels) * 4
    if len(payload) != expected:
        raise ParseError(f"{kind}: {len(payload)} payload bytes, expected {expected}")
    return Packet(kind, channels, count, index, rate, t0, payload)


def pcm_samples(packet: Packet) -> np.ndarray:
    """(count, channels) int16."""
    return np.frombuffer(packet.payload, dtype="<i2").reshape(packet.count, packet.channels)


def imu_records(packet: Packet):
    """(times float64 (count,), values float32 (count, channels))."""
    records = np.frombuffer(packet.payload, dtype="<f4").reshape(packet.count, 1 + packet.channels)
    return packet.t0 + records[:, 0].astype(np.float64), records[:, 1:]


def text(packet: Packet) -> str:
    return packet.payload.decode("utf-8", errors="replace")


def pack(kind: str, channels: int, count: int, index: int, rate: float, t0: float,
         payload: bytes) -> bytes:
    """Build a datagram; what the watch does, for fake_watch.py and tests."""
    return HEADER.pack(MAGIC, KIND_IDS[kind], channels, count, index & 0xFFFFFFFF,
                       rate, t0) + payload
