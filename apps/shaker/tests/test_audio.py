import time

import numpy as np
import pytest

from shaker.audio.bus import AudioBus
from shaker.audio.effects import (
    BrakeRumble,
    EngineRumble,
    GearShift,
    RevLimiter,
    RoadVibration,
    WheelSlip,
    apply_response_filter,
    gear_shift_rpm_factor,
)
from shaker.config import AudioConfig
from shaker.gt7.protocol import TelemetryPacket


def _active_packet() -> TelemetryPacket:
    """A packet that passes the bus's "is driving" filter."""
    p = TelemetryPacket()
    p.flags = 0b01  # on_track bit set, not paused
    p.lap_count = 1
    return p


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


def test_vibration_high_band_adds_energy_at_speed() -> None:
    """High-speed playback should sum more amplitude than low-speed at the same activity."""
    v_slow = RoadVibration(48000)
    v_fast = RoadVibration(48000)
    # Warm up the smoothers identically with activity but different speeds.
    for _ in range(40):
        slow = v_slow.process(480, activity=0.01, gain=1.0, enabled=True,
                              speed_mps=0.0, speed_blend_low_mps=20.0, speed_blend_high_mps=50.0)
        fast = v_fast.process(480, activity=0.01, gain=1.0, enabled=True,
                              speed_mps=80.0, speed_blend_low_mps=20.0, speed_blend_high_mps=50.0)
    # The fast variant gets the high band fully blended in, so RMS should be higher.
    assert float(np.sqrt(np.mean(fast ** 2))) > float(np.sqrt(np.mean(slow ** 2)))


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


def test_engine_rumble_silent_at_zero_throttle() -> None:
    e = EngineRumble(48000)
    # Drive a few callbacks so any startup transient settles.
    for _ in range(10):
        out = e.process(480, engine_rpm=3000.0, throttle=0, gain=1.0, enabled=True, rpm_divisor=60.0)
    assert np.max(np.abs(out)) < 1e-6


def test_engine_rumble_silent_below_min_rpm() -> None:
    e = EngineRumble(48000)
    out = e.process(480, engine_rpm=50.0, throttle=200, gain=1.0, enabled=True, rpm_divisor=60.0)
    assert np.max(np.abs(out)) == 0.0


def test_engine_rumble_silent_when_disabled() -> None:
    e = EngineRumble(48000)
    out = e.process(480, engine_rpm=3000.0, throttle=200, gain=1.0, enabled=False, rpm_divisor=60.0)
    assert np.max(np.abs(out)) == 0.0


def test_engine_rumble_produces_audio_at_throttle() -> None:
    e = EngineRumble(48000)
    for _ in range(20):  # let the smoother build up
        out = e.process(480, engine_rpm=3000.0, throttle=200, gain=1.0, enabled=True, rpm_divisor=60.0)
    assert np.max(np.abs(out)) > 0.1


def test_audio_bus_engine_sweep_returns_synthetic() -> None:
    bus = AudioBus(AudioConfig())
    bus.trigger_test_engine_sweep(duration_s=0.3, peak_rpm=6000.0)
    # Just after trigger: near start (envelope ≈ 0, but >0 within first frame).
    rpm0, t0 = bus.current_engine_state()
    assert 0.0 <= rpm0 <= 6000.0
    assert 0 <= t0 <= 255
    # After the duration: back to features.
    time.sleep(0.4)
    rpm1, t1 = bus.current_engine_state()
    assert rpm1 == bus.features.engine_rpm
    assert t1 == bus.features.throttle


def test_audio_bus_engine_sweep_peaks_at_midpoint() -> None:
    bus = AudioBus(AudioConfig())
    bus.trigger_test_engine_sweep(duration_s=0.2, peak_rpm=6000.0)
    time.sleep(0.1)  # halfway through
    rpm, throttle = bus.current_engine_state()
    # Triangular envelope peaks at 1.0 at the midpoint.
    assert rpm > 5000.0
    assert throttle > 200


# --- Brake / rev limiter / wheel slip effects --------------------------------


def test_brake_rumble_silent_below_threshold() -> None:
    b = BrakeRumble(48000)
    for _ in range(10):
        out = b.process(480, brake=30, gain=1.0, enabled=True, freq_hz=30.0, threshold_pct=20.0)
    assert np.max(np.abs(out)) < 1e-6


def test_brake_rumble_produces_audio_above_threshold() -> None:
    b = BrakeRumble(48000)
    for _ in range(30):
        out = b.process(480, brake=200, gain=1.0, enabled=True, freq_hz=30.0, threshold_pct=20.0)
    assert np.max(np.abs(out)) > 0.1


def test_brake_rumble_silent_when_disabled() -> None:
    b = BrakeRumble(48000)
    out = b.process(480, brake=255, gain=1.0, enabled=False, freq_hz=30.0, threshold_pct=20.0)
    assert np.max(np.abs(out)) == 0.0


def test_rev_limiter_silent_below_trigger() -> None:
    r = RevLimiter(48000)
    for _ in range(10):
        out = r.process(480, rpm_pct=0.8, gain=1.0, enabled=True, freq_hz=75.0, trigger_pct=95.0)
    assert np.max(np.abs(out)) < 1e-6


def test_rev_limiter_produces_audio_above_trigger() -> None:
    r = RevLimiter(48000)
    for _ in range(30):
        out = r.process(480, rpm_pct=0.99, gain=1.0, enabled=True, freq_hz=75.0, trigger_pct=95.0)
    assert np.max(np.abs(out)) > 0.1


def test_wheel_slip_silent_below_threshold() -> None:
    s = WheelSlip(48000)
    for _ in range(10):
        out = s.process(480, slip_magnitude=1.0, gain=1.0, enabled=True,
                        freq_hz=90.0, threshold_mps=2.0, scale_mps=5.0)
    assert np.max(np.abs(out)) < 1e-6


def test_wheel_slip_produces_audio_above_threshold() -> None:
    s = WheelSlip(48000)
    for _ in range(30):
        out = s.process(480, slip_magnitude=6.0, gain=1.0, enabled=True,
                        freq_hz=90.0, threshold_mps=2.0, scale_mps=5.0)
    assert np.max(np.abs(out)) > 0.1


def test_audio_bus_computes_slip_magnitude() -> None:
    bus = AudioBus(AudioConfig())
    p = _active_packet()
    p.speed_mps = 30.0
    # Three corners match speed (radius 0.3 m × rps 100 = 30 m/s); FL is spinning.
    p.tire_radius_FL = p.tire_radius_FR = p.tire_radius_RL = p.tire_radius_RR = 0.3
    p.wheel_rps_FR = p.wheel_rps_RL = p.wheel_rps_RR = 100.0
    p.wheel_rps_FL = 130.0  # 39 m/s wheel-surface speed
    bus.push_packet(p)
    assert bus.features.slip_magnitude == pytest.approx(9.0, abs=1e-3)


def test_audio_bus_captures_brake_input() -> None:
    bus = AudioBus(AudioConfig())
    p = _active_packet()
    p.brake = 180
    bus.push_packet(p)
    assert bus.features.brake == 180


def test_audio_bus_mute_defaults_off() -> None:
    bus = AudioBus(AudioConfig())
    assert bus.muted is False


def test_audio_bus_mute_is_in_memory_only() -> None:
    """Mute is intentionally not part of AudioConfig — flipping it doesn't
    touch the config and so it can't accidentally pollute a saved profile."""
    bus = AudioBus(AudioConfig())
    bus.muted = True
    # Pushing packets doesn't unmute or persist mute through features.
    p = _active_packet()
    bus.push_packet(p)
    assert bus.muted is True
    bus.muted = False
    assert bus.muted is False


def test_audio_bus_brake_test_override_peaks_at_midpoint() -> None:
    bus = AudioBus(AudioConfig())
    bus.trigger_test_brake_rumble(duration_s=0.2, peak_brake=200)
    time.sleep(0.1)
    assert bus.current_brake() > 150
    time.sleep(0.2)
    assert bus.current_brake() == bus.features.brake


def test_audio_bus_rev_limiter_test_override_crosses_redline() -> None:
    bus = AudioBus(AudioConfig())
    bus.trigger_test_rev_limiter(duration_s=0.2)
    time.sleep(0.1)
    assert bus.current_rpm_pct() > 0.9
    time.sleep(0.2)
    assert bus.current_rpm_pct() == bus.features.engine_rpm_pct


def test_audio_bus_slip_test_override_peaks_at_midpoint() -> None:
    bus = AudioBus(AudioConfig())
    bus.trigger_test_wheel_slip(duration_s=0.2, peak_slip_mps=8.0)
    time.sleep(0.1)
    assert bus.current_slip_magnitude() > 6.0
    time.sleep(0.2)
    assert bus.current_slip_magnitude() == bus.features.slip_magnitude


def test_audio_bus_detects_upshift_and_downshift() -> None:
    bus = AudioBus(AudioConfig())
    p = _active_packet()
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
    p = _active_packet()
    p.current_gear = 3
    bus.push_packet(p)
    p.current_gear = 0  # to neutral — ignore
    bus.push_packet(p)
    p.current_gear = 1  # from neutral — ignore
    bus.push_packet(p)
    assert bus.gear_shift_count == 0


def test_audio_bus_detects_reverse_engagement() -> None:
    bus = AudioBus(AudioConfig())
    p = _active_packet()
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
    p = _active_packet()
    for _ in range(10):
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.05
        bus.push_packet(p)
    settled = bus.features.suspension_activity
    p.suspension_FL = 0.2
    p.suspension_FR = 0.18
    bus.push_packet(p)
    assert bus.features.suspension_activity > settled


def test_audio_bus_hpf_rejects_constant_load() -> None:
    """Sustained equal load on all four corners (e.g., parked) should not generate vibration."""
    bus = AudioBus(AudioConfig())
    p = _active_packet()
    for _ in range(60):  # ~1 second of constant input
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.1
        bus.push_packet(p)
    # Should settle to ~zero (HPF blocks DC, envelope decays).
    assert bus.features.suspension_activity < 1e-3


def test_audio_bus_hpf_isolates_single_corner_hit() -> None:
    """A single-wheel kerb strike should produce activity even when other corners are quiet."""
    bus = AudioBus(AudioConfig())
    p = _active_packet()
    for _ in range(60):
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.05
        bus.push_packet(p)
    assert bus.features.suspension_activity < 1e-3  # settled

    # Only FL hits a kerb.
    p.suspension_FL = 0.30
    bus.push_packet(p)
    assert bus.features.suspension_activity > 0.05


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
    p = _active_packet()
    p.max_alert_rpm = 8000
    p.engine_rpm = 4000.0
    bus.push_packet(p)
    assert bus.features.engine_rpm_pct == pytest.approx(0.5)
    p.engine_rpm = 12000.0  # past redline
    bus.push_packet(p)
    assert bus.features.engine_rpm_pct == pytest.approx(1.0)


def test_audio_bus_rpm_percentage_zero_when_max_unknown() -> None:
    bus = AudioBus(AudioConfig())
    p = _active_packet()
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
    p = _active_packet()
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
    p = _active_packet()
    p.current_gear = 3
    bus.push_packet(p)

    menu = TelemetryPacket()
    menu.lap_count = -1
    menu.current_gear = 5  # ignored
    bus.push_packet(menu)
    assert bus.gear_shift_count == 0  # menu packet shouldn't have counted

    # Resume in 1st gear: should NOT fire a shift just because last active
    # state was 3rd gear, because the tracker was cleared.
    resume = _active_packet()
    resume.current_gear = 1
    bus.push_packet(resume)
    assert bus.gear_shift_count == 0


def test_audio_bus_ignores_paused_packet() -> None:
    bus = AudioBus(AudioConfig())
    p = _active_packet()
    p.engine_rpm = 4000.0
    p.max_alert_rpm = 8000
    bus.push_packet(p)
    assert bus.features.engine_rpm == 4000.0

    paused = _active_packet()
    paused.flags = 0b11  # on_track + paused
    paused.engine_rpm = 4000.0
    bus.push_packet(paused)
    assert bus.features.engine_rpm == 0.0  # reset


def test_audio_bus_reset_features_clears_derived_state() -> None:
    bus = AudioBus(AudioConfig())
    # Build up some state from a few active packets.
    p = _active_packet()
    p.max_alert_rpm = 8000
    p.engine_rpm = 4000.0
    p.throttle = 200
    p.brake = 50
    p.wheel_rps_FL = 50.0
    p.tire_radius_FL = 0.3
    p.suspension_FL = 0.05
    for _ in range(5):
        bus.push_packet(p)
    assert bus.features.engine_rpm == 4000.0

    # Reset (simulating "telemetry went stale").
    bus.reset_features()
    assert bus.features == type(bus.features)()  # back to defaults
    assert bus.features.engine_rpm == 0.0
    assert bus.features.throttle == 0
    assert bus.features.brake == 0


def test_audio_bus_reset_features_idempotent() -> None:
    bus = AudioBus(AudioConfig())
    bus.reset_features()
    bus.reset_features()
    bus.reset_features()
    assert bus.features.engine_rpm == 0.0


def test_audio_bus_ignores_off_track_packet() -> None:
    bus = AudioBus(AudioConfig())
    p = _active_packet()
    p.engine_rpm = 4000.0
    p.max_alert_rpm = 8000
    bus.push_packet(p)
    assert bus.features.engine_rpm == 4000.0

    off = _active_packet()
    off.flags = 0b00  # off track — between sessions
    off.engine_rpm = 4000.0
    bus.push_packet(off)
    assert bus.features.engine_rpm == 0.0  # reset


# --- Frozen-payload gate ------------------------------------------------------
# GT7's pause-menu exit leaves on_track set with the payload frozen at the
# last driving frame — the flag gate passes, so the freeze gate must catch it.

from shaker.audio.bus import _FREEZE_RESET_FRAMES  # noqa: E402


def _driving_packet(frame: int) -> TelemetryPacket:
    """An on-track packet whose physics vary per frame, like real driving."""
    p = _active_packet()
    p.engine_rpm = 4500.0 + frame
    p.speed_mps = 47.0 + frame * 0.01
    p.position_x = 100.0 + frame * 0.8
    p.throttle = 200
    p.max_alert_rpm = 8000
    p.wheel_rps_FL = 130.0
    p.tire_radius_FL = 0.3
    return p


def test_audio_bus_frozen_payload_resets_features() -> None:
    bus = AudioBus(AudioConfig())
    frozen = _driving_packet(0)
    for _ in range(_FREEZE_RESET_FRAMES + 1):
        bus.push_packet(frozen)
    assert bus.features.engine_rpm == 0.0
    assert bus.features.slip_magnitude == 0.0
    assert bus.features.throttle == 0


def test_audio_bus_varying_payload_is_not_frozen() -> None:
    bus = AudioBus(AudioConfig())
    for frame in range(_FREEZE_RESET_FRAMES * 3):
        bus.push_packet(_driving_packet(frame))
    assert bus.features.engine_rpm > 0.0
    assert bus.features.throttle == 200


def test_audio_bus_single_blip_restarts_freeze_window() -> None:
    """One changed frame mid-freeze (seen in the wild at the exit boundary)
    must not let stale data through indefinitely — only restart the count."""
    bus = AudioBus(AudioConfig())
    for _ in range(_FREEZE_RESET_FRAMES - 1):
        bus.push_packet(_driving_packet(0))
    bus.push_packet(_driving_packet(1))  # blip
    assert bus.features.engine_rpm > 0.0  # not yet frozen
    for _ in range(_FREEZE_RESET_FRAMES + 1):
        bus.push_packet(_driving_packet(1))
    assert bus.features.engine_rpm == 0.0  # re-froze and reset


def test_audio_bus_recovers_after_freeze() -> None:
    bus = AudioBus(AudioConfig())
    for _ in range(_FREEZE_RESET_FRAMES + 1):
        bus.push_packet(_driving_packet(0))
    assert bus.features.engine_rpm == 0.0
    # Driving resumes — features repopulate immediately.
    bus.push_packet(_driving_packet(1))
    assert bus.features.engine_rpm > 0.0
