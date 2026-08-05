"""Mixer tests: gain staging, limiting, and mute.

AudioOutput defers the sounddevice import into run(), so the callback can be
driven directly with a plain numpy buffer on any machine — no PortAudio, no
device. This whole path previously had no test coverage at all.
"""

from __future__ import annotations

import numpy as np
import pytest

from shaker.audio.bus import AudioBus, TelemetryFeatures
from shaker.audio.stream import AudioOutput
from shaker.config import AudioConfig

FRAMES = 960  # 20 ms at 48 kHz, the shipped default


def _everything_at_once() -> TelemetryFeatures:
    """Turn-in under braking with a lockup, on a rough surface, at the limiter.

    Not a contrived maximum — this is a real corner entry, and it is exactly
    when the driver most needs the cues to stay distinct.
    """
    return TelemetryFeatures(
        speed_mps=60.0,
        suspension_activity=0.05,
        engine_rpm=7000.0,
        engine_rpm_pct=0.99,
        throttle=255,
        brake=200,
        slip_magnitude=9.0,
    )


def _drive(
    cfg: AudioConfig,
    features: TelemetryFeatures,
    blocks: int = 200,
    gear_every: int = 0,
) -> tuple[AudioBus, np.ndarray]:
    bus = AudioBus(cfg)
    bus.features = features
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 1), dtype=np.float32)
    rendered = []
    for i in range(blocks):
        if gear_every and i % gear_every == 0:
            bus.gear_shift_count += 1
        out._callback(buf, FRAMES, None, None)
        rendered.append(buf[:, 0].copy())
    return bus, np.concatenate(rendered)


def test_output_never_exceeds_full_scale() -> None:
    _, x = _drive(AudioConfig(), _everything_at_once(), gear_every=25)
    assert np.max(np.abs(x)) <= 1.0


def test_limiter_keeps_clipping_negligible_at_defaults() -> None:
    """Six effects at gain 1.0 sum well past unity; hard-clipping that mix
    folded intermodulation products into 120-300 Hz, where the clean mix has
    essentially no energy. Measured 20-27% of samples clipped before limiting."""
    _, x = _drive(AudioConfig(), _everything_at_once(), gear_every=25)
    clipped = float(np.mean(np.abs(x) >= 0.999))
    assert clipped < 0.01, f"{clipped:.2%} of samples clipped"


def test_limiter_holds_up_at_extreme_master_gain() -> None:
    """master_gain goes to 2.0 in the UI."""
    _, x = _drive(AudioConfig(master_gain=2.0), _everything_at_once(), gear_every=25)
    assert np.max(np.abs(x)) <= 1.0
    assert float(np.mean(np.abs(x) >= 0.999)) < 0.02


def test_quiet_mix_passes_through_unlimited() -> None:
    """The limiter must not be riding gain during ordinary driving."""
    gentle = TelemetryFeatures(speed_mps=30.0, engine_rpm=2500.0,
                               engine_rpm_pct=0.4, throttle=90)
    bus, x = _drive(AudioConfig(), gentle, blocks=100)
    assert np.max(np.abs(x)) < 0.95


def test_mute_ramps_rather_than_cutting() -> None:
    bus = AudioBus(AudioConfig())
    bus.features = _everything_at_once()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 1), dtype=np.float32)
    for _ in range(80):
        out._callback(buf, FRAMES, None, None)
    before = buf[:, 0].copy()

    bus.muted = True
    out._callback(buf, FRAMES, None, None)
    seam = abs(float(buf[0, 0]) - float(before[-1]))
    assert seam < 0.05, f"mute stepped {seam:.4f} full-scale"


def test_mute_reaches_true_silence() -> None:
    bus = AudioBus(AudioConfig())
    bus.features = _everything_at_once()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 1), dtype=np.float32)
    for _ in range(40):
        out._callback(buf, FRAMES, None, None)
    bus.muted = True
    for _ in range(40):
        out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, 0])) == 0.0


def test_unmute_ramps_back_up() -> None:
    bus = AudioBus(AudioConfig())
    bus.features = _everything_at_once()
    bus.muted = True
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 1), dtype=np.float32)
    for _ in range(40):
        out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, 0])) == 0.0

    bus.muted = False
    out._callback(buf, FRAMES, None, None)
    assert abs(float(buf[0, 0])) < 0.05  # starts from silence, not a step
    for _ in range(40):
        out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, 0])) > 0.1  # and comes back


def test_silent_telemetry_produces_silence() -> None:
    """Stale telemetry resets features to zero; output must settle to nothing."""
    _, x = _drive(AudioConfig(), TelemetryFeatures(), blocks=120)
    assert np.max(np.abs(x[-FRAMES:])) < 1e-4


def test_callback_is_continuous_across_blocks() -> None:
    """Whole-mixer version of the per-effect continuity checks."""
    bus = AudioBus(AudioConfig())
    bus.features = _everything_at_once()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 1), dtype=np.float32)
    blocks = []
    for _ in range(120):
        out._callback(buf, FRAMES, None, None)
        blocks.append(buf[:, 0].copy())
    seams = [abs(float(b[0] - a[-1])) for a, b in zip(blocks[20:], blocks[21:])]
    inner = max(float(np.max(np.abs(np.diff(b)))) for b in blocks[20:])
    # A seam should be no worse than an ordinary step inside a block.
    assert max(seams) <= inner * 1.5, f"seam {max(seams):.4f} vs in-block {inner:.4f}"


def test_engine_frequency_is_clamped_into_the_shaker_band() -> None:
    """A redline pull must not walk the carrier out of the felt band.

    At the default divisor of 60, 7000 rpm is 117 Hz and a 13k-rpm car is
    217 Hz — radiated as airborne buzz through the frame, not felt.
    """
    cfg = AudioConfig(vibration_enabled=False, brake_rumble_enabled=False,
                      rev_limiter_enabled=False, wheel_slip_enabled=False,
                      gear_shift_enabled=False)
    _, x = _drive(cfg, TelemetryFeatures(engine_rpm=13000.0, throttle=255,
                                         engine_rpm_pct=0.9), blocks=100)
    tail = x[-48000:]
    power = np.abs(np.fft.rfft(tail)) ** 2
    freqs = np.fft.rfftfreq(len(tail), 1.0 / 48000)
    dominant = float(freqs[int(np.argmax(power))])
    assert dominant == pytest.approx(cfg.engine_rumble_max_hz, abs=2.0)
