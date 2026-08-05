"""Two-channel front/rear output: routing, pan law, and the mono fallback.

Channel 0 drives the front shaker (pedal deck), channel 1 the rear (seat).

The property these tests exist to protect is that `output_channels = 1` is a
true escape hatch: a single field flipped back has to reproduce the
single-channel rig exactly, not approximately, so a rollback is provably a
no-op rather than a new tuning problem.
"""

from __future__ import annotations

import numpy as np
import pytest

from shaker.audio.bus import AudioBus, TelemetryFeatures
from shaker.audio.stream import _pan, AudioOutput
from shaker.config import AudioConfig

FRAMES = 960
FRONT, REAR = 0, 1


def _busy() -> TelemetryFeatures:
    """Corner entry: rough surface, front locking, rear spinning, on the limiter."""
    return TelemetryFeatures(
        speed_mps=55.0,
        suspension_activity=0.03,
        suspension_activity_front=0.03,
        suspension_activity_rear=0.02,
        engine_rpm=6500.0,
        engine_rpm_pct=0.98,
        throttle=200,
        brake=210,
        slip_magnitude=8.0,
        slip_front=-8.0,   # locked
        slip_rear=6.0,     # spinning
    )


def _drive(cfg: AudioConfig, features: TelemetryFeatures, blocks: int = 120,
           muted: bool = False) -> np.ndarray:
    """Render and return an (n, channels) array."""
    bus = AudioBus(cfg)
    bus.features = features
    bus.muted = muted
    out = AudioOutput(bus)
    channels = out._out_channels
    buf = np.zeros((FRAMES, channels), dtype=np.float32)
    rendered = []
    for _ in range(blocks):
        out._callback(buf, FRAMES, None, None)
        rendered.append(buf.copy())
    return np.concatenate(rendered)


def _stereo_cfg(**over) -> AudioConfig:
    return AudioConfig(output_channels=2, **over)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)))


# --- Pan law -----------------------------------------------------------------


def test_pan_law_is_unity_sum() -> None:
    """Weights must sum to 1 at every bias — that invariant is what makes the
    mono path an exact passthrough rather than an approximation."""
    for bias in (-1.0, -0.7, -0.25, 0.0, 0.3, 0.5, 1.0):
        front, rear = _pan(bias)
        assert front + rear == pytest.approx(1.0)
        assert front >= 0.0 and rear >= 0.0


def test_pan_law_extremes_are_exclusive() -> None:
    assert _pan(-1.0) == (1.0, 0.0)
    assert _pan(1.0) == (0.0, 1.0)
    assert _pan(0.0) == (0.5, 0.5)


def test_pan_law_clamps_out_of_range_bias() -> None:
    """Config is hand-editable over SSH; a typo must not invert a channel."""
    assert _pan(-5.0) == (1.0, 0.0)
    assert _pan(5.0) == (0.0, 1.0)


# --- Routing -----------------------------------------------------------------


def _only_brake(**over) -> AudioConfig:
    return _stereo_cfg(
        vibration_enabled=False, engine_rumble_enabled=False,
        rev_limiter_enabled=False, wheel_slip_enabled=False,
        gear_shift_enabled=False, **over,
    )


def test_full_front_bias_leaves_the_rear_silent() -> None:
    x = _drive(_only_brake(brake_rumble_bias=-1.0), _busy())
    assert _rms(x[:, FRONT]) > 0.01
    assert np.max(np.abs(x[:, REAR])) < 1e-6


def test_full_rear_bias_leaves_the_front_silent() -> None:
    x = _drive(_only_brake(brake_rumble_bias=1.0), _busy())
    assert _rms(x[:, REAR]) > 0.01
    assert np.max(np.abs(x[:, FRONT])) < 1e-6


def test_centred_bias_splits_evenly() -> None:
    x = _drive(_only_brake(brake_rumble_bias=0.0), _busy())
    assert _rms(x[:, FRONT]) == pytest.approx(_rms(x[:, REAR]), rel=1e-3)


def test_brake_defaults_to_the_front_shaker() -> None:
    """Pad judder and ABS reach a real driver through the pedal."""
    x = _drive(_only_brake(), _busy())
    assert _rms(x[:, FRONT]) > _rms(x[:, REAR]) * 3


def test_gear_shift_defaults_to_the_rear_shaker() -> None:
    """Driveline shock reacts through the driven axle."""
    cfg = _stereo_cfg(
        vibration_enabled=False, engine_rumble_enabled=False,
        brake_rumble_enabled=False, rev_limiter_enabled=False,
        wheel_slip_enabled=False,
    )
    bus = AudioBus(cfg)
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    rendered = []
    for i in range(60):
        if i == 5:
            bus.gear_shift_count += 1
        out._callback(buf, FRAMES, None, None)
        rendered.append(buf.copy())
    x = np.concatenate(rendered)
    assert _rms(x[:, REAR]) > _rms(x[:, FRONT]) * 2


def test_slip_is_routed_by_axle_not_by_bias() -> None:
    """Front lockup and rear wheelspin are separate events on separate shakers.

    There is no slip bias field on purpose — the axle comes from real telemetry,
    so a knob could only corrupt it with a guess.
    """
    cfg = _stereo_cfg(
        vibration_enabled=False, engine_rumble_enabled=False,
        brake_rumble_enabled=False, rev_limiter_enabled=False,
        gear_shift_enabled=False,
    )
    front_only = TelemetryFeatures(speed_mps=55.0, slip_front=-8.0, slip_rear=0.0)
    x = _drive(cfg, front_only)
    assert _rms(x[:, FRONT]) > 0.01
    assert np.max(np.abs(x[:, REAR])) < 1e-6

    rear_only = TelemetryFeatures(speed_mps=55.0, slip_front=0.0, slip_rear=8.0)
    y = _drive(cfg, rear_only)
    assert _rms(y[:, REAR]) > 0.01
    assert np.max(np.abs(y[:, FRONT])) < 1e-6


def test_lockup_and_spin_use_different_frequencies() -> None:
    """A locked tyre grinds lower than a spinning one scrabbles."""
    cfg = _stereo_cfg(
        vibration_enabled=False, engine_rumble_enabled=False,
        brake_rumble_enabled=False, rev_limiter_enabled=False,
        gear_shift_enabled=False,
    )
    x = _drive(cfg, TelemetryFeatures(speed_mps=55.0, slip_front=-8.0, slip_rear=8.0))

    def dominant(chan: np.ndarray) -> float:
        tail = chan[-48000:]
        power = np.abs(np.fft.rfft(tail)) ** 2
        freqs = np.fft.rfftfreq(len(tail), 1.0 / 48000)
        return float(freqs[int(np.argmax(power))])

    assert dominant(x[:, FRONT]) == pytest.approx(cfg.wheel_slip_lock_freq_hz, abs=2.0)
    assert dominant(x[:, REAR]) == pytest.approx(cfg.wheel_slip_freq_hz, abs=2.0)


def test_road_vibration_channels_are_decorrelated() -> None:
    """Both ends driven by the identical waveform excites the rig frame's
    bounce mode and starves the pitch mode — the one carrying front-vs-rear."""
    cfg = _stereo_cfg(
        engine_rumble_enabled=False, brake_rumble_enabled=False,
        rev_limiter_enabled=False, wheel_slip_enabled=False,
        gear_shift_enabled=False,
    )
    x = _drive(cfg, TelemetryFeatures(speed_mps=40.0, suspension_activity_front=0.02,
                                      suspension_activity_rear=0.02), blocks=200)
    front, rear = x[48000:, FRONT], x[48000:, REAR]
    corr = float(np.corrcoef(front, rear)[0, 1])
    assert abs(corr) < 0.25, f"channels too correlated: {corr:.3f}"


# --- Rear trim and the shared limiter ---------------------------------------


def test_rear_trim_scales_only_the_rear() -> None:
    cfg = _only_brake(brake_rumble_bias=0.0)
    base = _drive(cfg, _busy())
    trimmed = _drive(_only_brake(brake_rumble_bias=0.0, rear_gain_trim=0.5), _busy())
    assert _rms(trimmed[:, FRONT]) == pytest.approx(_rms(base[:, FRONT]), rel=0.02)
    assert _rms(trimmed[:, REAR]) == pytest.approx(_rms(base[:, REAR]) * 0.5, rel=0.02)


def test_limiter_holds_the_front_rear_ratio_under_load() -> None:
    """Limiting each channel independently would shift the balance rearward
    exactly when the driver most needs to feel the front loading up."""
    quiet = _drive(_stereo_cfg(master_gain=0.3), _busy())
    loud = _drive(_stereo_cfg(master_gain=2.0), _busy())
    quiet_ratio = _rms(quiet[:, REAR]) / _rms(quiet[:, FRONT])
    loud_ratio = _rms(loud[:, REAR]) / _rms(loud[:, FRONT])
    assert loud_ratio == pytest.approx(quiet_ratio, rel=0.05)


def test_stereo_never_exceeds_full_scale() -> None:
    x = _drive(_stereo_cfg(master_gain=2.0), _busy())
    assert np.max(np.abs(x)) <= 1.0


# --- Mute --------------------------------------------------------------------


def test_mute_silences_both_columns() -> None:
    """PortAudio hands back a view on its own buffer and never clears it, so a
    second column left untouched would replay its last block forever."""
    bus = AudioBus(_stereo_cfg())
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    for _ in range(40):
        out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, REAR])) > 0.0

    bus.muted = True
    for _ in range(60):
        out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, FRONT])) == 0.0
    assert np.max(np.abs(buf[:, REAR])) == 0.0


# --- Wiring check ------------------------------------------------------------


def test_wiring_check_pulses_one_channel_at_a_time() -> None:
    bus = AudioBus(_stereo_cfg())
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_wiring_check(pulse_s=0.2, gap_s=0.1)

    both_at_once = False
    order: list[str] = []
    for _ in range(60):
        out._callback(buf, FRAMES, None, None)
        # The block that ends the check also renders the normal mix, which
        # legitimately drives both channels — stop before sampling it.
        if out._wiring_frames < 0:
            break
        f = float(np.max(np.abs(buf[:, FRONT])))
        r = float(np.max(np.abs(buf[:, REAR])))
        if f > 0.05 and r > 0.05:
            both_at_once = True
        for label, level in (("front", f), ("rear", r)):
            if level > 0.05 and (not order or order[-1] != label):
                order.append(label)
    assert not both_at_once, "front and rear overlapped; the check cannot prove ordering"
    assert order == ["front", "rear"], f"expected one front pulse then one rear, got {order}"


def test_wiring_check_bypasses_the_mix() -> None:
    """The pulse has to be provably the only thing playing, or 'did the rear
    fire?' is answered against a background of road noise."""
    bus = AudioBus(_stereo_cfg())
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_wiring_check(pulse_s=0.2, gap_s=0.1)
    out._callback(buf, FRAMES, None, None)
    # Only the front column carries anything during the first pulse.
    assert np.max(np.abs(buf[:, REAR])) == 0.0


def test_wiring_check_respects_mute() -> None:
    bus = AudioBus(_stereo_cfg())
    bus.features = _busy()
    bus.muted = True
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_wiring_check(pulse_s=0.2, gap_s=0.1)
    out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf)) == 0.0


def test_normal_mix_resumes_after_the_wiring_check() -> None:
    bus = AudioBus(_stereo_cfg())
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_wiring_check(pulse_s=0.05, gap_s=0.02)
    for _ in range(30):
        out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, FRONT])) > 0.0
    assert np.max(np.abs(buf[:, REAR])) > 0.0


# --- Mono fallback -----------------------------------------------------------


def test_mono_config_opens_one_channel() -> None:
    out = AudioOutput(AudioBus(AudioConfig()))
    assert out._out_channels == 1
    assert out._vibration_rear is None
    assert out._slip_rear is None


def test_mono_writes_only_column_zero() -> None:
    """A single-shaker rig must be untouched by two-channel support existing."""
    bus = AudioBus(AudioConfig())
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)  # deliberately oversized
    for _ in range(40):
        out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, FRONT])) > 0.0
    assert np.max(np.abs(buf[:, REAR])) == 0.0


def test_mono_ignores_bias_and_trim() -> None:
    """Placement fields must be inert on one channel, not half-applied."""
    plain = _drive(AudioConfig(), _busy())
    fiddled = _drive(
        AudioConfig(brake_rumble_bias=1.0, gear_shift_bias=-1.0,
                    engine_rumble_bias=-1.0, rear_gain_trim=0.1),
        _busy(),
    )
    assert np.array_equal(plain, fiddled)


def test_stereo_voices_share_the_noise_buffers() -> None:
    """Two 10 s bands are 3.8 MB; copying them per voice would double it."""
    bus = AudioBus(_stereo_cfg())
    out = AudioOutput(bus)
    assert out._vibration._noise_low is out._vibration_rear._noise_low
    assert out._vibration._noise_high is out._vibration_rear._noise_high
    assert out._vibration._cursor != out._vibration_rear._cursor
