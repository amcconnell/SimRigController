"""Placing the gear-shift and engine effects from the car's drivetrain.

GT7 sends a car code but nothing about the car. Looking that code up decides
which end of the rig a shift thump and the engine thrum belong on — the two
effects that are otherwise a fixed guess, and that guess is simply wrong for a
front-wheel-drive car.

The design under test: the configured bias supplies the *magnitude*, the car
database supplies the *direction*. An unrecognised car must change nothing at
all, because "silently did nothing" and "worked" are otherwise indistinguishable.
"""

from __future__ import annotations

import numpy as np
import pytest

from shaker.audio.bus import AudioBus, TelemetryFeatures
from shaker.audio.stream import AudioOutput, _pan
from shaker.config import AudioConfig
from shaker.gt7.protocol import TelemetryPacket, parse_packet

FRAMES = 960
FRONT, REAR = 0, 1

# Real GT7 car codes from the vendored table.
FR_CAR = 24      # Nissan 180SX Type X '96 — front engine, rear drive
FWD_CAR = 37     # front engine, front drive
MID_CAR = 116    # mid engine, rear drive
AWD_CAR = 3246   # Bugatti Veyron Gr.4 — four wheel drive
UNKNOWN_CAR = 999999


def _shift_only(**over) -> AudioConfig:
    return AudioConfig(
        output_channels=2, vibration_enabled=False, engine_rumble_enabled=False,
        brake_rumble_enabled=False, rev_limiter_enabled=False,
        wheel_slip_enabled=False, **over,
    )


def _render_shift(cfg: AudioConfig, car_code: int | None) -> np.ndarray:
    bus = AudioBus(cfg)
    bus.features = TelemetryFeatures(speed_mps=40.0, engine_rpm=5000.0, engine_rpm_pct=0.8)
    bus.car_code = car_code
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    rendered = []
    for i in range(50):
        if i == 3:
            bus.gear_shift_count += 1
        out._callback(buf, FRAMES, None, None)
        rendered.append(buf.copy())
    return np.concatenate(rendered)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)))


# --- The lookup drives placement --------------------------------------------


def test_rear_drive_car_thumps_the_seat() -> None:
    x = _render_shift(_shift_only(), FR_CAR)
    assert _rms(x[:, REAR]) > _rms(x[:, FRONT]) * 2


def test_front_drive_car_thumps_the_pedals() -> None:
    """The case today's fixed default gets wrong: an FF car's driveline shock
    reaches the driver through the front axle, not the seat."""
    x = _render_shift(_shift_only(), FWD_CAR)
    assert _rms(x[:, FRONT]) > _rms(x[:, REAR]) * 2


def test_four_wheel_drive_shifts_are_centred() -> None:
    x = _render_shift(_shift_only(), AWD_CAR)
    assert _rms(x[:, FRONT]) == pytest.approx(_rms(x[:, REAR]), rel=1e-3)


def test_configured_bias_still_sets_the_strength() -> None:
    """Direction comes from the car, magnitude stays the user's."""
    strong = _render_shift(_shift_only(gear_shift_bias=1.0), FWD_CAR)
    mild = _render_shift(_shift_only(gear_shift_bias=0.2), FWD_CAR)
    # Both go front, but the strong one leaves less behind in the rear.
    assert _rms(strong[:, REAR]) < _rms(mild[:, REAR])
    assert np.max(np.abs(strong[:, REAR])) < 1e-6  # bias 1.0 -> fully front


def test_a_negative_configured_bias_is_not_fought_by_the_lookup() -> None:
    """Magnitude is taken as an absolute, so a user who set the slider to the
    'wrong' side still gets the car-correct end at the strength they chose."""
    x = _render_shift(_shift_only(gear_shift_bias=-0.9), FR_CAR)
    assert _rms(x[:, REAR]) > _rms(x[:, FRONT]) * 2


# --- Fail-safe ---------------------------------------------------------------


def test_unknown_car_changes_nothing() -> None:
    """Must be byte-identical to routing being off, or an unrecognised car is
    indistinguishable from a broken lookup."""
    unknown = _render_shift(_shift_only(), UNKNOWN_CAR)
    disabled = _render_shift(_shift_only(drivetrain_routing_enabled=False), UNKNOWN_CAR)
    assert np.array_equal(unknown, disabled)


def test_no_car_code_yet_changes_nothing() -> None:
    """Before the first packet there is no car to look up."""
    none_yet = _render_shift(_shift_only(), None)
    disabled = _render_shift(_shift_only(drivetrain_routing_enabled=False), None)
    assert np.array_equal(none_yet, disabled)


def test_disabling_the_lookup_restores_the_raw_slider() -> None:
    """With routing off, an FF car must obey the configured rear bias — proving
    the switch really is a switch."""
    x = _render_shift(_shift_only(drivetrain_routing_enabled=False), FWD_CAR)
    assert _rms(x[:, REAR]) > _rms(x[:, FRONT]) * 2


def test_routing_is_inert_on_a_single_channel_rig() -> None:
    cfg = AudioConfig(  # mono
        vibration_enabled=False, engine_rumble_enabled=False,
        brake_rumble_enabled=False, rev_limiter_enabled=False,
        wheel_slip_enabled=False,
    )
    bus_a, bus_b = AudioBus(cfg), AudioBus(cfg)
    outs = []
    for bus, car in ((bus_a, FWD_CAR), (bus_b, FR_CAR)):
        bus.features = TelemetryFeatures(speed_mps=40.0, engine_rpm=5000.0, engine_rpm_pct=0.8)
        bus.car_code = car
        out = AudioOutput(bus)
        buf = np.zeros((FRAMES, 1), dtype=np.float32)
        acc = []
        for i in range(50):
            if i == 3:
                bus.gear_shift_count += 1
            out._callback(buf, FRAMES, None, None)
            acc.append(buf.copy())
        outs.append(np.concatenate(acc))
    assert np.array_equal(outs[0], outs[1])


# --- Engine placement --------------------------------------------------------


def _engine_only(**over) -> AudioConfig:
    return AudioConfig(
        output_channels=2, vibration_enabled=False, brake_rumble_enabled=False,
        rev_limiter_enabled=False, wheel_slip_enabled=False,
        gear_shift_enabled=False, **over,
    )


def _render_engine(cfg: AudioConfig, car_code: int | None) -> np.ndarray:
    bus = AudioBus(cfg)
    bus.features = TelemetryFeatures(speed_mps=40.0, engine_rpm=4000.0,
                                     engine_rpm_pct=0.6, throttle=220)
    bus.car_code = car_code
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    rendered = []
    for _ in range(60):
        out._callback(buf, FRAMES, None, None)
        rendered.append(buf.copy())
    return np.concatenate(rendered)


def test_mid_engine_car_thrums_the_seat() -> None:
    x = _render_engine(_engine_only(), MID_CAR)
    # Checked against the pan law rather than a guessed ratio: the default
    # magnitude of 0.3 means 0.65 rear against 0.35 front, so the split is
    # 1.86:1 by construction and a round threshold would be arbitrary.
    front_w, rear_w = _pan(AudioConfig().engine_rumble_bias)
    assert _rms(x[:, REAR]) / _rms(x[:, FRONT]) == pytest.approx(rear_w / front_w, rel=0.02)


def test_front_engine_car_thrums_the_pedals() -> None:
    x = _render_engine(_engine_only(), FR_CAR)
    front_w, rear_w = _pan(AudioConfig().engine_rumble_bias)
    assert _rms(x[:, FRONT]) / _rms(x[:, REAR]) == pytest.approx(rear_w / front_w, rel=0.02)


def test_engine_placement_is_mirrored_between_layouts() -> None:
    """A mid-engine car and a front-engine one must be exact mirrors of each
    other — same magnitude, opposite ends."""
    mid = _render_engine(_engine_only(), MID_CAR)
    front = _render_engine(_engine_only(), FR_CAR)
    assert _rms(mid[:, REAR]) == pytest.approx(_rms(front[:, FRONT]), rel=1e-3)
    assert _rms(mid[:, FRONT]) == pytest.approx(_rms(front[:, REAR]), rel=1e-3)


def test_four_wheel_drive_engine_position_is_left_alone() -> None:
    """The source records drive type, not engine position, and 4WD spans both
    extremes — so the configured value must survive untouched rather than be
    guessed at."""
    routed = _render_engine(_engine_only(), AWD_CAR)
    raw = _render_engine(_engine_only(drivetrain_routing_enabled=False), AWD_CAR)
    assert np.array_equal(routed, raw)


# --- The car code itself -----------------------------------------------------


def test_car_code_is_parsed_within_the_existing_length_guarantee() -> None:
    """0x124 is an i32, so it needs bytes through 0x127 — and _MIN_PARSE_LEN is
    already 0x128. Parsing it adds no new length requirement and cannot break
    the shorter packet layouts."""
    data = bytearray(b"0S7G") + bytearray(0x128 - 4)
    data[0x124:0x128] = (1234).to_bytes(4, "little")
    assert parse_packet(bytes(data)).car_code == 1234


def test_bus_tracks_the_car_code_from_packets() -> None:
    bus = AudioBus(AudioConfig())
    assert bus.car_code is None
    p = TelemetryPacket()
    p.flags = 0b01
    p.lap_count = 1
    p.car_code = FR_CAR
    bus.push_packet(p)
    assert bus.car_code == FR_CAR


def test_car_code_survives_a_menu_reset() -> None:
    """Which car you are in does not stop being true because the game paused —
    clearing it would make the rig re-derive its routing on every menu."""
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    p.flags = 0b01
    p.lap_count = 1
    p.car_code = MID_CAR
    bus.push_packet(p)
    bus.reset_features()
    assert bus.car_code == MID_CAR
