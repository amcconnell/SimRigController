import time

import numpy as np
import pytest

from shaker.audio.bus import AudioBus
from shaker.audio.effects import (
    GearShift,
    RoadVibration,
    apply_response_filter,
    gear_shift_rpm_factor,
)
from shaker.config import AudioConfig
from shaker.gt7.protocol import TelemetryPacket


def test_vibration_silent_when_activity_zero() -> None:
    v = RoadVibration(48000)
    out = v.process(480, activity=0.0, gain=1.0, enabled=True)
    assert out.shape == (480,)
    assert np.max(np.abs(out)) < 1e-6


def test_vibration_produces_audio_when_activity_present() -> None:
    v = RoadVibration(48000)
    for _ in range(20):
        out = v.process(480, activity=0.01, gain=1.0, enabled=True)
    assert np.max(np.abs(out)) > 0.01


def test_vibration_disabled_yields_silence() -> None:
    v = RoadVibration(48000)
    out = v.process(480, activity=0.01, gain=1.0, enabled=False)
    assert np.max(np.abs(out)) == 0.0


def test_gear_shift_idle_is_silent() -> None:
    g = GearShift(48000)
    out = g.process(480, count=0, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08)
    assert np.max(np.abs(out)) == 0.0


def test_gear_shift_triggers_on_count_advance() -> None:
    g = GearShift(48000)
    g.process(480, count=0, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08)
    out = g.process(480, count=1, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08)
    assert np.max(np.abs(out)) > 0.1


def test_audio_bus_detects_upshift_and_downshift() -> None:
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    p.current_gear = 1
    bus.push_packet(p)
    assert bus.gear_shift_count == 0
    p.current_gear = 2  # upshift
    bus.push_packet(p)
    assert bus.gear_shift_count == 1
    p.current_gear = 3  # upshift
    bus.push_packet(p)
    assert bus.gear_shift_count == 2
    p.current_gear = 2  # downshift
    bus.push_packet(p)
    assert bus.gear_shift_count == 3


def test_audio_bus_ignores_neutral_transitions() -> None:
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    p.current_gear = 3
    bus.push_packet(p)
    p.current_gear = 0  # to neutral — ignore
    bus.push_packet(p)
    p.current_gear = 1  # from neutral — ignore
    bus.push_packet(p)
    assert bus.gear_shift_count == 0


def test_audio_bus_detects_reverse_engagement() -> None:
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    p.current_gear = 1
    bus.push_packet(p)
    p.current_gear = 15  # forward -> reverse
    bus.push_packet(p)
    assert bus.gear_shift_count == 1
    p.current_gear = 1   # reverse -> forward
    bus.push_packet(p)
    assert bus.gear_shift_count == 2


def test_audio_bus_suspension_activity_responds_to_bumps() -> None:
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    for _ in range(10):
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.05
        bus.push_packet(p)
    settled = bus.features.suspension_activity
    p.suspension_FL = 0.2
    p.suspension_FR = 0.18
    bus.push_packet(p)
    assert bus.features.suspension_activity > settled


def test_test_vibration_burst_overrides_idle_activity() -> None:
    bus = AudioBus(AudioConfig())
    assert bus.current_vibration_activity() == 0.0
    bus.trigger_test_vibration(duration_s=0.2)
    assert bus.current_vibration_activity() > 0.0
    time.sleep(0.25)
    assert bus.current_vibration_activity() == 0.0


def test_test_gear_shift_increments_counter() -> None:
    bus = AudioBus(AudioConfig())
    assert bus.gear_shift_count == 0
    bus.trigger_test_gear_shift()
    assert bus.gear_shift_count == 1


_RPM_ARGS = (0.5, 0.9, 0.5, 1.0)  # low_pct, high_pct, min_gain, max_gain


def test_gear_rpm_factor_flat_below_low_threshold() -> None:
    assert gear_shift_rpm_factor(-0.5, *_RPM_ARGS) == pytest.approx(0.5)
    assert gear_shift_rpm_factor(0.0, *_RPM_ARGS) == pytest.approx(0.5)
    assert gear_shift_rpm_factor(0.49, *_RPM_ARGS) == pytest.approx(0.5)
    assert gear_shift_rpm_factor(0.50, *_RPM_ARGS) == pytest.approx(0.5)


def test_gear_rpm_factor_flat_above_high_threshold() -> None:
    assert gear_shift_rpm_factor(0.90, *_RPM_ARGS) == pytest.approx(1.0)
    assert gear_shift_rpm_factor(1.0, *_RPM_ARGS) == pytest.approx(1.0)
    assert gear_shift_rpm_factor(1.5, *_RPM_ARGS) == pytest.approx(1.0)


def test_gear_rpm_factor_ramps_between_thresholds() -> None:
    assert gear_shift_rpm_factor(0.70, *_RPM_ARGS) == pytest.approx(0.75)
    assert gear_shift_rpm_factor(0.60, *_RPM_ARGS) == pytest.approx(0.625)


def test_gear_rpm_factor_honors_custom_anchors() -> None:
    # Different curve: ramp 20% -> 80% revs, gain 0.0 -> 2.0
    args = (0.2, 0.8, 0.0, 2.0)
    assert gear_shift_rpm_factor(0.0, *args) == pytest.approx(0.0)
    assert gear_shift_rpm_factor(0.5, *args) == pytest.approx(1.0)  # halfway
    assert gear_shift_rpm_factor(1.0, *args) == pytest.approx(2.0)


def test_audio_bus_computes_rpm_percentage_from_max_alert() -> None:
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    p.max_alert_rpm = 8000
    p.engine_rpm = 4000.0
    bus.push_packet(p)
    assert bus.features.engine_rpm_pct == pytest.approx(0.5)
    p.engine_rpm = 12000.0  # past redline
    bus.push_packet(p)
    assert bus.features.engine_rpm_pct == pytest.approx(1.0)


def test_audio_bus_rpm_percentage_zero_when_max_unknown() -> None:
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    p.max_alert_rpm = 0
    p.engine_rpm = 5000.0
    bus.push_packet(p)
    assert bus.features.engine_rpm_pct == 0.0


# --- Response filter ---------------------------------------------------------


def test_response_filter_neutral_defaults_pass_through() -> None:
    # input_gain=1, threshold=0, min_force=0, gamma=1 → output equals input
    for x in (0.0, 0.001, 0.25, 0.5, 0.75, 1.0):
        assert apply_response_filter(x, 1.0, 0.0, 0.0, 1.0) == pytest.approx(x)


def test_response_filter_threshold_gates_below() -> None:
    assert apply_response_filter(0.4, 1.0, 0.5, 0.0, 1.0) == 0.0
    assert apply_response_filter(0.5, 1.0, 0.5, 0.0, 1.0) == 0.0
    # Above threshold, linear remap [0.5, 1] → [0, 1]
    assert apply_response_filter(0.75, 1.0, 0.5, 0.0, 1.0) == pytest.approx(0.5)
    assert apply_response_filter(1.0, 1.0, 0.5, 0.0, 1.0) == pytest.approx(1.0)


def test_response_filter_min_force_floors_output() -> None:
    # Just above gate → output ≈ min_force
    assert apply_response_filter(0.0001, 1.0, 0.0, 0.3, 1.0) == pytest.approx(0.3, abs=1e-3)
    # Full input → 1.0 regardless of min_force
    assert apply_response_filter(1.0, 1.0, 0.0, 0.3, 1.0) == pytest.approx(1.0)


def test_response_filter_gamma_bends_curve() -> None:
    # gamma=2 squares the input
    assert apply_response_filter(0.5, 1.0, 0.0, 0.0, 2.0) == pytest.approx(0.25)
    # gamma=0.5 takes square root
    assert apply_response_filter(0.25, 1.0, 0.0, 0.0, 0.5) == pytest.approx(0.5)


def test_response_filter_input_gain_amplifies() -> None:
    # input_gain=2 doubles input before clip
    assert apply_response_filter(0.3, 2.0, 0.0, 0.0, 1.0) == pytest.approx(0.6)
    # Clipped to 1.0 if amplified above
    assert apply_response_filter(0.8, 2.0, 0.0, 0.0, 1.0) == pytest.approx(1.0)


def test_response_filter_threshold_at_or_above_one_silences() -> None:
    assert apply_response_filter(1.0, 1.0, 1.0, 0.0, 1.0) == 0.0
    assert apply_response_filter(0.5, 1.0, 1.5, 0.0, 1.0) == 0.0


# --- Ignore packets when out of session --------------------------------------


def test_audio_bus_ignores_packet_with_negative_lap_count() -> None:
    bus = AudioBus(AudioConfig())
    # Build up some state from a real packet first.
    p = TelemetryPacket()
    p.lap_count = 1
    p.engine_rpm = 5000.0
    p.max_alert_rpm = 9000
    p.suspension_FL = 0.05
    bus.push_packet(p)
    assert bus.features.engine_rpm == 5000.0

    # A menu/replay packet (lap_count < 0) should reset features and not
    # advance any state.
    menu = TelemetryPacket()
    menu.lap_count = -1
    menu.engine_rpm = 1234.0
    bus.push_packet(menu)
    assert bus.features.engine_rpm == 0.0
    assert bus.features.suspension_activity == 0.0
    assert bus.features.lap_count == 0


def test_audio_bus_negative_lap_resets_gear_tracker() -> None:
    bus = AudioBus(AudioConfig())
    p = TelemetryPacket()
    p.lap_count = 1
    p.current_gear = 3
    bus.push_packet(p)

    menu = TelemetryPacket()
    menu.lap_count = -1
    menu.current_gear = 5  # ignored
    bus.push_packet(menu)
    assert bus.gear_shift_count == 0  # menu packet shouldn't have counted

    # Resume in 1st gear: should NOT fire a shift just because last active
    # state was 3rd gear, because the tracker was cleared.
    resume = TelemetryPacket()
    resume.lap_count = 1
    resume.current_gear = 1
    bus.push_packet(resume)
    assert bus.gear_shift_count == 0
