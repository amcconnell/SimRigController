"""GT7 UDP telemetry: Salsa20 decryption + binary packet parsing.

Protocol details derived from gt_telem / PDTools community work.
This module was ported from the racetrace project (track/telemetry.py)
and trimmed to the fields the shaker needs.

GT7 broadcasts 60 Hz telemetry over UDP on port 33740 once it has
received a heartbeat ('A') on port 33739.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

GT7_RECEIVE_PORT = 33739
GT7_BIND_PORT = 33740
PACKET_RATE_HZ = 60

HEARTBEAT = b"A"

_HEADER_LE = b"0S7G"
_HEADER_BE = b"G6S0"

_SALSA_KEY = b"Simulator Interface Packet GT7 ver 0.0"
_MIN_DECRYPT_LEN = 0x44
_MIN_PARSE_LEN = 0x128


def decrypt_packet(data: bytes) -> bytes:
    """Decrypt a GT7 telemetry packet using Salsa20.

    The IV is derived from a seed at byte offset 0x40:
      iv = pack_le(seed ^ 0xDEADBEAF, seed)
    """
    from Crypto.Cipher import Salsa20

    if len(data) < _MIN_DECRYPT_LEN:
        raise ValueError(f"Packet too short: {len(data)} bytes")

    seed = struct.unpack_from("<I", data, 0x40)[0]
    iv = seed ^ 0xDEADBEAF
    iv_bytes = struct.pack("<II", iv, seed)
    key = _SALSA_KEY[:32]
    return Salsa20.new(key=key, nonce=iv_bytes).decrypt(data)


@dataclass
class TelemetryPacket:
    """Parsed fields from a single GT7 UDP telemetry packet."""

    # Position (m) — left-handed coords; Y is elevation
    position_x: float = 0.0
    position_y: float = 0.0
    position_z: float = 0.0

    # Body velocity (m/s)
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    velocity_z: float = 0.0

    # Angular velocity (rad/s) — pitch / yaw / roll
    ang_vel_x: float = 0.0
    ang_vel_y: float = 0.0
    ang_vel_z: float = 0.0

    body_height: float = 0.0
    engine_rpm: float = 0.0
    speed_mps: float = 0.0

    # Per-corner wheel rotation (revolutions per second)
    wheel_rps_FL: float = 0.0
    wheel_rps_FR: float = 0.0
    wheel_rps_RL: float = 0.0
    wheel_rps_RR: float = 0.0

    # Per-corner tire radius (m) — multiply by wheel_rps for m/s
    tire_radius_FL: float = 0.317
    tire_radius_FR: float = 0.317
    tire_radius_RL: float = 0.317
    tire_radius_RR: float = 0.317

    # Per-corner suspension travel (m)
    suspension_FL: float = 0.0
    suspension_FR: float = 0.0
    suspension_RL: float = 0.0
    suspension_RR: float = 0.0

    # Lap / packet metadata
    packet_id: int = 0
    lap_count: int = 0
    best_lap_ms: int = -1
    last_lap_ms: int = -1

    flags: int = 0
    throttle: int = 0
    brake: int = 0
    current_gear: int = 0
    suggested_gear: int = 0

    # Per-car alert RPMs (yellow / red shift lights). max_alert_rpm is the redline.
    min_alert_rpm: int = 0
    max_alert_rpm: int = 0

    # Engine / drivetrain
    boost_pressure: float = 0.0
    oil_pressure: float = 0.0
    water_temp: float = 0.0
    oil_temp: float = 0.0
    fuel_level: float = 0.0
    fuel_capacity: float = 0.0

    # Road surface plane
    road_plane_x: float = 0.0
    road_plane_y: float = 0.0
    road_plane_z: float = 0.0
    road_plane_dist: float = 0.0


def parse_packet(data: bytes) -> TelemetryPacket:
    """Parse a decrypted GT7 packet into a TelemetryPacket."""
    if len(data) < _MIN_PARSE_LEN:
        raise ValueError(f"Packet too short after decryption: {len(data)} bytes")

    header = data[:4]
    if header == _HEADER_LE:
        endian = "<"
    elif header == _HEADER_BE:
        endian = ">"
    else:
        raise ValueError(f"Invalid packet header: {header!r}")

    def f(offset: int) -> float:
        return struct.unpack_from(f"{endian}f", data, offset)[0]

    def i32(offset: int) -> int:
        return struct.unpack_from(f"{endian}i", data, offset)[0]

    def i16(offset: int) -> int:
        return struct.unpack_from(f"{endian}h", data, offset)[0]

    def u8(offset: int) -> int:
        return struct.unpack_from("B", data, offset)[0]

    p = TelemetryPacket()

    p.position_x = f(0x04)
    p.position_y = f(0x08)
    p.position_z = f(0x0C)
    p.velocity_x = f(0x10)
    p.velocity_y = f(0x14)
    p.velocity_z = f(0x18)

    p.ang_vel_x = f(0x2C)
    p.ang_vel_y = f(0x30)
    p.ang_vel_z = f(0x34)

    p.body_height = f(0x38)
    p.engine_rpm = f(0x3C)
    p.fuel_level = f(0x44)
    p.fuel_capacity = f(0x48)
    p.speed_mps = f(0x4C)
    p.boost_pressure = f(0x50)
    p.oil_pressure = f(0x54)
    p.water_temp = f(0x58)
    p.oil_temp = f(0x5C)

    p.packet_id = i32(0x70)
    p.lap_count = i16(0x74)
    p.best_lap_ms = i32(0x78)
    p.last_lap_ms = i32(0x7C)

    p.min_alert_rpm = i16(0x88)
    p.max_alert_rpm = i16(0x8A)
    p.flags = i16(0x8E)
    gear_byte = u8(0x90)
    p.current_gear = gear_byte & 0x0F
    p.suggested_gear = (gear_byte >> 4) & 0x0F
    p.throttle = u8(0x91)
    p.brake = u8(0x92)

    p.road_plane_x = f(0x94)
    p.road_plane_y = f(0x98)
    p.road_plane_z = f(0x9C)
    p.road_plane_dist = f(0xA0)

    p.wheel_rps_FL = f(0xA4)
    p.wheel_rps_FR = f(0xA8)
    p.wheel_rps_RL = f(0xAC)
    p.wheel_rps_RR = f(0xB0)

    p.tire_radius_FL = f(0xB4)
    p.tire_radius_FR = f(0xB8)
    p.tire_radius_RL = f(0xBC)
    p.tire_radius_RR = f(0xC0)

    p.suspension_FL = f(0xC4)
    p.suspension_FR = f(0xC8)
    p.suspension_RL = f(0xCC)
    p.suspension_RR = f(0xD0)

    return p


def is_paused(p: TelemetryPacket) -> bool:
    """True if GT7 is paused (bit 1 of flags)."""
    return bool(p.flags & (1 << 1))


def is_on_track(p: TelemetryPacket) -> bool:
    """True if the car is on track (bit 0 of flags)."""
    return bool(p.flags & (1 << 0))
