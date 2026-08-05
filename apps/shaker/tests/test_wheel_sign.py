"""Wheel rotation sign, pinned against real console measurements.

GT7 sends per-corner wheel rotation negated: driving forward, the raw value
times tire radius equals *minus* the vehicle speed. That was measured on a live
PS5 on 2026-08-05 and is normalized on parse.

Why this file exists: for the entire life of the project, `slip_magnitude` read
`2 x speed` at all times, so the wheel-slip effect sat pinned at full amplitude
whenever the car moved. Every test passed throughout, because they all built
`TelemetryPacket` objects directly and were self-consistent with the same wrong
assumption. These tests go through `parse_packet` with bytes laid out the way
the console actually sends them, and assert on physics rather than on internals.

Numbers below are real samples from that session, not invented.
"""

from __future__ import annotations

import struct

import pytest

from shaker.audio.bus import AudioBus
from shaker.config import AudioConfig
from shaker.gt7.protocol import parse_packet

_RADIUS = 0.317
_PACKET_LEN = 0x128


def _raw_packet(speed_mps: float, surface_mps: tuple[float, float, float, float]) -> bytes:
    """A decrypted packet as GT7 lays one out, in GT7's own sign convention.

    `surface_mps` is the true forward surface speed of FL/FR/RL/RR. The console
    stores rotation negated, so that is what gets written to the wire.
    """
    buf = bytearray(_PACKET_LEN)
    buf[0:4] = b"0S7G"
    struct.pack_into("<f", buf, 0x4C, speed_mps)
    struct.pack_into("<h", buf, 0x74, 1)       # lap_count
    struct.pack_into("<h", buf, 0x8E, 0b01)    # on track, not paused
    for i, surface in enumerate(surface_mps):
        struct.pack_into("<f", buf, 0xA4 + 4 * i, -surface / _RADIUS)  # negated
        struct.pack_into("<f", buf, 0xB4 + 4 * i, _RADIUS)
    return bytes(buf)


def _slip(speed_mps: float, surface: tuple[float, float, float, float]) -> AudioBus:
    bus = AudioBus(AudioConfig())
    bus.push_packet(parse_packet(_raw_packet(speed_mps, surface)))
    return bus


# --- The measurement this file is named for ---------------------------------


def test_forward_rolling_wheel_reads_positive_surface_speed() -> None:
    p = parse_packet(_raw_packet(30.0, (30.0, 30.0, 30.0, 30.0)))
    assert p.wheel_rps_FL * p.tire_radius_FL == pytest.approx(30.0, rel=1e-4)


def test_coasting_produces_almost_no_slip() -> None:
    """51.0 m/s coasting, all four wheels tracking the car.

    This is the case that was broken: it used to report 102 m/s of slip, which
    pinned the effect at full amplitude at every speed above 12 km/h.
    """
    bus = _slip(51.0, (50.99, 50.99, 50.50, 50.50))
    assert bus.features.slip_magnitude < 1.0
    assert abs(bus.features.slip_front) < 1.0
    assert abs(bus.features.slip_rear) < 1.0


def test_slip_does_not_scale_with_speed() -> None:
    """The signature of the old bug: slip tracked speed at a ratio of -2.00."""
    for speed in (1.78, 6.47, 24.39, 43.78, 51.0):
        bus = _slip(speed, (speed, speed, speed, speed))
        assert bus.features.slip_magnitude < 0.5, f"slip scales with speed at {speed} m/s"


# --- Real samples, decoded --------------------------------------------------


def test_threshold_braking_reads_as_lockup_on_both_axles() -> None:
    """43.78 m/s, 100% brake. Both axles drag; the front drags harder."""
    bus = _slip(43.78, (38.79, 38.79, 40.45, 40.45))
    assert bus.features.slip_front == pytest.approx(-4.99, abs=0.05)
    assert bus.features.slip_rear == pytest.approx(-3.33, abs=0.05)
    # Front brake bias plus forward load transfer: the front must slip more.
    assert abs(bus.features.slip_front) > abs(bus.features.slip_rear)


def test_first_gear_launch_reads_as_rear_wheelspin() -> None:
    """17.14 m/s, 100% throttle, first gear, rear-wheel drive.

    The unambiguous corner-ordering check: only a driven wheel can outrun the
    car, so a positive rear and a near-zero front confirms the FL/FR/RL/RR
    offsets are the way round the code assumes.
    """
    bus = _slip(17.14, (17.14, 17.14, 24.76, 24.76))
    assert bus.features.slip_front == pytest.approx(0.0, abs=0.05)
    assert bus.features.slip_rear == pytest.approx(7.62, abs=0.05)


def test_lockup_and_spin_have_opposite_signs() -> None:
    """The whole point of keeping the sign: they are opposite corrections."""
    locking = _slip(40.0, (35.0, 35.0, 40.0, 40.0))
    spinning = _slip(40.0, (40.0, 40.0, 46.0, 46.0))
    assert locking.features.slip_front < 0
    assert spinning.features.slip_rear > 0


# --- Effect behaviour -------------------------------------------------------


def test_wheel_slip_effect_is_silent_while_merely_driving() -> None:
    """End-to-end: an ordinary cruise must not trigger the slip effect.

    With the sign inverted this produced a full-amplitude 90 Hz tone at every
    speed above 12 km/h — a constant drone rather than a cue, consuming the
    headroom the limiter then took back off every other effect.
    """
    import numpy as np

    from shaker.audio.stream import AudioOutput

    bus = _slip(50.0, (50.0, 50.0, 50.0, 50.0))
    cfg = AudioConfig(
        vibration_enabled=False, engine_rumble_enabled=False,
        brake_rumble_enabled=False, rev_limiter_enabled=False,
        gear_shift_enabled=False,  # wheel slip only
    )
    bus.audio_config = cfg
    out = AudioOutput(bus)
    buf = np.zeros((960, 1), dtype=np.float32)
    for _ in range(60):
        out._callback(buf, 960, None, None)
    assert np.max(np.abs(buf)) < 1e-4, "slip effect is droning during a plain cruise"
