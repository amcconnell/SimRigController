"""Audio effect generators. Each returns mono float32 samples per callback."""

from __future__ import annotations

import numpy as np

# Road vibration uses two pre-rendered bandpass-noise loops:
#  - Low band: dominant content, present at all speeds (matches a tuned shaker).
#  - High band: faded in with speed so the felt content gets richer/sharper at pace.
_VIB_LOW_BAND_FREQ_LOW_HZ = 44.0
_VIB_LOW_BAND_FREQ_HIGH_HZ = 50.0
_VIB_HIGH_BAND_FREQ_LOW_HZ = 60.0
_VIB_HIGH_BAND_FREQ_HIGH_HZ = 80.0
_VIB_NOISE_DURATION_S = 10.0  # pre-rendered noise loop length
_VIB_ACTIVITY_SCALE = 200.0   # maps suspension_activity to 0..1 modulation


def apply_response_filter(
    x: float,
    input_gain: float,
    threshold: float,
    min_force: float,
    gamma: float,
) -> float:
    """SimHub-style input→force shaping. All inputs/outputs in 0..1 (gain unbounded)."""
    x = max(0.0, min(1.0, x * input_gain))
    if threshold >= 1.0 or x <= threshold:
        return 0.0
    y = (x - threshold) / (1.0 - threshold)
    y = min_force + (1.0 - min_force) * y
    return max(0.0, min(1.0, y ** max(gamma, 1e-3)))

def gear_shift_rpm_factor(
    rpm_pct: float,
    low_pct: float,
    high_pct: float,
    min_gain: float,
    max_gain: float,
) -> float:
    """Multiplier on gear_shift_gain. All inputs are 0..1 fractions."""
    if rpm_pct <= low_pct:
        return min_gain
    if rpm_pct >= high_pct:
        return max_gain
    t = (rpm_pct - low_pct) / (high_pct - low_pct)
    return min_gain + (max_gain - min_gain) * t


def _bandpass_noise(sample_rate: int, duration_s: float, low_hz: float, high_hz: float, seed: int) -> np.ndarray:
    """FFT-based bandpass noise: white -> rfft -> zero out-of-band bins -> irfft."""
    n = int(sample_rate * duration_s)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    spectrum = np.fft.rfft(noise)
    spectrum[(freqs < low_hz) | (freqs > high_hz)] = 0
    out = np.fft.irfft(spectrum, n).astype(np.float32)
    peak = float(np.max(np.abs(out)))
    if peak > 0:
        out /= peak
    return out


class RoadVibration:
    """Continuous bandpass-noise vibration modulated by suspension activity.

    Two noise bands: a low band always present (matches a user-tuned shaker
    frequency), and a high band whose contribution scales with vehicle speed.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sr = sample_rate
        self._noise_low = _bandpass_noise(
            sample_rate, _VIB_NOISE_DURATION_S,
            _VIB_LOW_BAND_FREQ_LOW_HZ, _VIB_LOW_BAND_FREQ_HIGH_HZ, seed=1,
        )
        self._noise_high = _bandpass_noise(
            sample_rate, _VIB_NOISE_DURATION_S,
            _VIB_HIGH_BAND_FREQ_LOW_HZ, _VIB_HIGH_BAND_FREQ_HIGH_HZ, seed=2,
        )
        self._cursor = 0
        self._smoothed_amp = 0.0
        self._smoothed_high_blend = 0.0

    def process(
        self,
        n_frames: int,
        activity: float,
        gain: float,
        enabled: bool,
        input_gain: float = 1.0,
        threshold: float = 0.0,
        min_force: float = 0.0,
        gamma: float = 1.0,
        speed_mps: float = 0.0,
        speed_blend_low_mps: float = 20.0,
        speed_blend_high_mps: float = 50.0,
    ) -> np.ndarray:
        if not enabled:
            self._smoothed_amp *= 0.5  # decay toward zero so re-enable is smooth
            return np.zeros(n_frames, dtype=np.float32)

        normalized = float(np.clip(activity * _VIB_ACTIVITY_SCALE, 0.0, 1.0))
        target = apply_response_filter(normalized, input_gain, threshold, min_force, gamma)
        # smooth amplitude per-callback to avoid zipper noise
        alpha = 0.35
        self._smoothed_amp = alpha * target + (1 - alpha) * self._smoothed_amp

        # Speed → high-band blend factor (0..1), smoothed across callbacks.
        span = speed_blend_high_mps - speed_blend_low_mps
        if span <= 0:
            high_target = 0.0
        else:
            high_target = float(np.clip((speed_mps - speed_blend_low_mps) / span, 0.0, 1.0))
        self._smoothed_high_blend = 0.1 * high_target + 0.9 * self._smoothed_high_blend

        # Wrap-read from both noise buffers at a shared cursor.
        n = self._noise_low.size
        start = self._cursor
        end = start + n_frames
        if end <= n:
            low_chunk = self._noise_low[start:end]
            high_chunk = self._noise_high[start:end]
        else:
            first = n - start
            low_chunk = np.concatenate((self._noise_low[start:], self._noise_low[: n_frames - first]))
            high_chunk = np.concatenate((self._noise_high[start:], self._noise_high[: n_frames - first]))
        self._cursor = end % n

        mix = low_chunk + high_chunk * self._smoothed_high_blend
        return mix * (self._smoothed_amp * gain)


class EngineRumble:
    """Continuous-phase sine driven by engine RPM, amplitude-modulated by throttle.

    Felt vibration of a running engine — present at idle, growing with throttle.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sr = sample_rate
        self._phase = 0.0  # radians, accumulated across callbacks for continuity
        self._smoothed_amp = 0.0

    def process(
        self,
        n_frames: int,
        engine_rpm: float,
        throttle: int,
        gain: float,
        enabled: bool,
        rpm_divisor: float,
        min_rpm: float = 100.0,
    ) -> np.ndarray:
        out = np.zeros(n_frames, dtype=np.float32)
        if not enabled or engine_rpm < min_rpm or rpm_divisor <= 0:
            self._smoothed_amp *= 0.5  # decay so re-enable / re-rev is smooth
            return out

        freq_hz = engine_rpm / rpm_divisor
        target = (throttle / 255.0) * gain

        alpha = 0.3
        self._smoothed_amp = alpha * target + (1.0 - alpha) * self._smoothed_amp

        omega = 2.0 * np.pi * freq_hz / self.sr
        phases = self._phase + omega * np.arange(n_frames, dtype=np.float32)
        out[:] = np.sin(phases) * self._smoothed_amp
        self._phase = float((self._phase + omega * n_frames) % (2.0 * np.pi))
        return out


class GearShift:
    """Short percussive thump retriggered when the bus' shift counter advances.

    Frequency and duration are captured at trigger time so that mid-thump config
    changes don't cause a discontinuity in the envelope.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sr = sample_rate
        self._last_count = 0
        self._envelope_pos = -1
        self._envelope_len = 0
        self._freq_hz = 0.0

    def process(
        self,
        n_frames: int,
        count: int,
        gain: float,
        enabled: bool,
        freq_hz: float,
        duration_s: float,
    ) -> np.ndarray:
        out = np.zeros(n_frames, dtype=np.float32)
        if not enabled:
            self._last_count = count
            self._envelope_pos = -1
            return out

        if count > self._last_count:
            self._envelope_pos = 0
            self._envelope_len = max(1, int(self.sr * duration_s))
            self._freq_hz = freq_hz
            self._last_count = count

        if self._envelope_pos < 0:
            return out

        remaining = self._envelope_len - self._envelope_pos
        n = min(remaining, n_frames)
        if n <= 0:
            self._envelope_pos = -1
            return out

        idx = self._envelope_pos + np.arange(n, dtype=np.float32)
        env = (1.0 - idx / self._envelope_len) ** 2
        t = idx / self.sr
        out[:n] = (env * np.sin(2.0 * np.pi * self._freq_hz * t)).astype(np.float32) * gain

        self._envelope_pos += n
        if self._envelope_pos >= self._envelope_len:
            self._envelope_pos = -1
        return out
