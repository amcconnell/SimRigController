"""Block-boundary continuity tests.

Every generator is called once per audio callback and must hand back a buffer
that joins seamlessly onto the previous one. A discontinuity at the seam is
broadband energy, and a bass shaker bolted to a rig frame radiates that as an
audible tick rather than felt motion — the artifact is *heard*, which is
exactly what the hardware is not supposed to do.

These tests all failed before the release-envelope work: the gated effects
returned a block of zeros while an internal envelope decayed, so coming off the
brakes stepped the output from full scale to silence in a single sample.
"""

from __future__ import annotations

import numpy as np
import pytest

from shaker.audio.effects import (
    BrakeRumble,
    EngineRumble,
    GearShift,
    RevLimiter,
    RoadVibration,
    WheelSlip,
)

SR = 48000

# A seam step this size is inaudible through a shaker. The pre-fix code
# measured up to 0.9999 — a full-scale step in one sample.
MAX_SEAM_STEP = 0.05


def _seam(prev: np.ndarray, nxt: np.ndarray) -> float:
    """Absolute discontinuity between the last sample of one block and the first of the next."""
    return abs(float(nxt[0]) - float(prev[-1]))


# Block sizes are swept so the gate lands at many different carrier phases.
# A single block size can accidentally land on a zero crossing and hide the
# defect entirely — at the 75 Hz rev-limiter default and a 20 ms block, 75 Hz
# is exactly 1.5 cycles per block, so every boundary sits at a zero crossing
# and the effect looks clean while being latently broken.
PHASE_SWEEP = range(940, 981)


def test_brake_release_is_continuous() -> None:
    worst = 0.0
    for n in PHASE_SWEEP:
        b = BrakeRumble(SR)
        for _ in range(60):
            prev = b.process(n, brake=220, gain=1.0, enabled=True,
                             freq_hz=30.0, threshold_pct=20.0).copy()
        nxt = b.process(n, brake=0, gain=1.0, enabled=True,
                        freq_hz=30.0, threshold_pct=20.0)
        worst = max(worst, _seam(prev, nxt))
    assert worst < MAX_SEAM_STEP, f"brake release steps {worst:.4f} full-scale"


def test_wheel_slip_release_is_continuous() -> None:
    worst = 0.0
    for n in PHASE_SWEEP:
        s = WheelSlip(SR)
        for _ in range(60):
            prev = s.process(n, slip=8.0, gain=1.0, enabled=True,
                             freq_hz=90.0, threshold_pct=8.0, scale_pct=12.0, speed_mps=50.0).copy()
        nxt = s.process(n, slip=0.0, gain=1.0, enabled=True,
                        freq_hz=90.0, threshold_pct=8.0, scale_pct=12.0, speed_mps=50.0)
        worst = max(worst, _seam(prev, nxt))
    assert worst < MAX_SEAM_STEP, f"slip release steps {worst:.4f} full-scale"


def test_rev_limiter_release_is_continuous() -> None:
    # 78 Hz rather than the 75 Hz default on purpose: 75 Hz hides the defect
    # behind a zero crossing at every 20 ms boundary.
    worst = 0.0
    for n in PHASE_SWEEP:
        r = RevLimiter(SR)
        for _ in range(60):
            prev = r.process(n, rpm_pct=0.99, gain=1.0, enabled=True,
                             freq_hz=78.0, trigger_pct=95.0).copy()
        nxt = r.process(n, rpm_pct=0.5, gain=1.0, enabled=True,
                        freq_hz=78.0, trigger_pct=95.0)
        worst = max(worst, _seam(prev, nxt))
    assert worst < MAX_SEAM_STEP, f"rev limiter release steps {worst:.4f} full-scale"


def test_engine_release_is_continuous_and_has_no_dc() -> None:
    """Losing telemetry mid-rev must fade, not cut — and must not leave DC.

    reset_features() zeroes engine_rpm, so a generator that re-derived its
    frequency on the release path would get 0 Hz and hold sin(constant) — a DC
    pedestal into the amp, worse than the click it replaced.
    """
    worst_seam = 0.0
    for n in PHASE_SWEEP:
        e = EngineRumble(SR)
        for _ in range(60):
            prev = e.process(n, engine_rpm=5000.0, throttle=255, gain=1.0,
                             enabled=True, rpm_divisor=60.0).copy()
        nxt = e.process(n, engine_rpm=0.0, throttle=0, gain=1.0,
                        enabled=True, rpm_divisor=60.0)
        worst_seam = max(worst_seam, _seam(prev, nxt))
    assert worst_seam < MAX_SEAM_STEP, f"engine release steps {worst_seam:.4f} full-scale"

    e = EngineRumble(SR)
    for _ in range(60):
        e.process(960, engine_rpm=5000.0, throttle=255, gain=1.0,
                  enabled=True, rpm_divisor=60.0)
    tail = np.concatenate([
        e.process(960, engine_rpm=0.0, throttle=0, gain=1.0,
                  enabled=True, rpm_divisor=60.0).copy()
        for _ in range(8)
    ])
    # Compared against the tail's own RMS, not an absolute level: a decaying
    # sinusoid has a small but real non-zero mean (~1/(omega*T)), whereas a
    # 0 Hz carrier holding sin(constant) gives mean ~= RMS. The ratio is what
    # separates "fading tone" from "DC into the amp".
    rms = float(np.sqrt(np.mean(tail ** 2)))
    assert rms > 0, "release tail is empty"
    assert abs(float(np.mean(tail))) < 0.25 * rms, "release tail carries a DC offset"


def test_disable_mid_effect_is_continuous() -> None:
    """Toggling an effect off in the UI must not click either."""
    for cls, kwargs in (
        (BrakeRumble, dict(brake=220, freq_hz=30.0, threshold_pct=20.0)),
        (WheelSlip, dict(slip=8.0, freq_hz=90.0, threshold_pct=8.0, scale_pct=12.0, speed_mps=50.0)),
        (RevLimiter, dict(rpm_pct=0.99, freq_hz=78.0, trigger_pct=95.0)),
    ):
        fx = cls(SR)
        for _ in range(60):
            prev = fx.process(960, gain=1.0, enabled=True, **kwargs).copy()
        nxt = fx.process(960, gain=1.0, enabled=False, **kwargs)
        assert _seam(prev, nxt) < MAX_SEAM_STEP, f"{cls.__name__} disable steps"


def test_road_vibration_activity_drop_is_continuous() -> None:
    v = RoadVibration(SR)
    for _ in range(60):
        prev = v.process(960, activity=0.02, gain=1.0, enabled=True).copy()
    nxt = v.process(960, activity=0.0, gain=1.0, enabled=True)
    assert _seam(prev, nxt) < MAX_SEAM_STEP


def test_gear_shift_retrigger_is_continuous() -> None:
    """A close-ratio downshift retriggers mid-thump; the tail must cross-fade.

    Checked at the longest duration the UI allows (500 ms), where a hard
    envelope reset measured a 0.845 step.
    """
    g = GearShift(SR)
    g.process(960, count=1, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.5)
    prev = g.process(960, count=1, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.5).copy()
    nxt = g.process(960, count=2, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.5)
    assert _seam(prev, nxt) < MAX_SEAM_STEP


def test_gear_shift_survives_counter_reset() -> None:
    """The bus counter restarts at 0 on service restart; shifts must still fire."""
    g = GearShift(SR)
    for count in (1, 2, 3):
        g.process(960, count=count, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08)
    out = g.process(960, count=1, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08)
    assert np.max(np.abs(out)) > 0.1, "shift after a counter reset was swallowed"


def test_gear_shift_gain_is_latched_at_trigger() -> None:
    """stream.py recomputes gear-shift gain from live RPM every block.

    Left unlatched, that stepped the thump's amplitude mid-transient and made
    the same shift feel different run to run.
    """
    steady = GearShift(SR)
    steady.process(480, count=1, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08)
    steady_blocks = [
        steady.process(480, count=1, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08).copy()
        for _ in range(6)
    ]
    swept = GearShift(SR)
    swept.process(480, count=1, gain=1.0, enabled=True, freq_hz=44.0, duration_s=0.08)
    swept_blocks = [
        swept.process(480, count=1, gain=0.5, enabled=True, freq_hz=44.0, duration_s=0.08).copy()
        for _ in range(6)
    ]
    for a, b in zip(steady_blocks, swept_blocks):
        assert a == pytest.approx(b), "gain change leaked into an in-flight thump"


def test_smoothing_is_independent_of_block_size() -> None:
    """The same wall-clock input must produce the same envelope at any buffer_ms.

    Time constants are re-derived from the real block duration, so audio.buffer_ms
    (exposed in the UI from 1 to 200 ms) is a latency control, not a hidden
    retune of every effect.
    """
    duration_s = 0.25
    window = int(0.1 * SR)  # 100 ms: three full cycles at 30 Hz
    levels = []
    for block in (240, 960, 2400):  # 5 ms, 20 ms, 50 ms
        b = BrakeRumble(SR)
        rendered = np.concatenate([
            b.process(block, brake=220, gain=1.0, enabled=True,
                      freq_hz=30.0, threshold_pct=20.0).copy()
            for _ in range(int(duration_s * SR / block))
        ])
        # RMS over a fixed wall-clock tail, so the measurement spans the same
        # span of the waveform regardless of how it was chunked.
        levels.append(float(np.sqrt(np.mean(rendered[-window:] ** 2))))
    assert levels[1] == pytest.approx(levels[0], rel=0.05)
    assert levels[2] == pytest.approx(levels[0], rel=0.05)


def test_road_vibration_preserves_bump_ordering() -> None:
    """A kerb, gravel and a heavy landing must not all read as the same hit.

    Full scale is activity=0.005 but a real single-corner kerb strike is ~0.05
    (see test_audio_bus_hpf_isolates_single_corner_hit), so a hard clip at 1.0
    deleted every distinction above a light bump.
    """
    levels = []
    for activity in (0.005, 0.008, 0.02, 0.05):
        v = RoadVibration(SR)
        for _ in range(80):
            out = v.process(960, activity=activity, gain=1.0, enabled=True)
        levels.append(float(np.sqrt(np.mean(out ** 2))))
    for quieter, louder in zip(levels, levels[1:]):
        assert louder > quieter * 1.02, f"bump magnitudes collapsed: {levels}"
