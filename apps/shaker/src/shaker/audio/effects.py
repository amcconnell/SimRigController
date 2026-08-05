"""Audio effect generators. Each returns mono float32 samples per callback.

Two invariants hold across every generator here, because violating either one
produces a broadband step that a tactile transducer radiates as a *tick heard
through the rig frame* rather than felt motion:

1. Amplitude is interpolated per sample across the block, never held as one
   scalar for all N samples. A held scalar steps at every block boundary.
2. A gated effect releases through its smoother and keeps rendering until it
   is genuinely inaudible. Returning a zero block while an internal envelope
   decays is a hard cut, not a fade.
"""

from __future__ import annotations

import math

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
# Both bands are normalized to this RMS so the speed blend is a crossfade at
# constant level. Peak-normalizing and *summing* them (the previous approach)
# raised the peak to 1.537 and RMS by 2.4 dB, so crossing the blend threshold
# made the road bed louder rather than sharper.
_VIB_BAND_RMS = 0.30

# Amplitude smoothing time constants, in seconds. These are time constants
# rather than per-callback alphas on purpose: the alpha is re-derived from the
# real block duration on every call, so the felt attack is identical whether
# audio.buffer_ms is 1 or 200. The previous hardcoded per-callback alphas
# silently retuned every effect whenever the buffer size changed.
_TAU_VIBRATION_S = 0.046
_TAU_HIGH_BLEND_S = 0.190
_TAU_ENGINE_S = 0.056
_TAU_BRAKE_S = 0.056
_TAU_REV_LIMITER_S = 0.033
_TAU_SLIP_S = 0.046
# Release toward silence. Must span at least ~2 carrier periods (66 ms at the
# 30 Hz brake default) or the fade itself splatters back into the buzz band.
_TAU_RELEASE_S = 0.029

# Below this amplitude nothing is felt through a shaker; stop rendering and
# hand back true silence so idle effects cost nothing.
_SILENCE = 1e-4


def _block_alpha(block_s: float, tau_s: float) -> float:
    """Per-block smoothing coefficient for a given time constant."""
    if tau_s <= 0.0:
        return 1.0
    return 1.0 - math.exp(-block_s / tau_s)


def _render_tone(
    phase: float,
    freq_hz: float,
    sample_rate: int,
    n_frames: int,
    amp_from: float,
    amp_to: float,
) -> tuple[np.ndarray, float]:
    """A sine block with per-sample amplitude ramp and continuous phase.

    Returns (samples, next_phase). The amplitude ramp is what removes the
    block-boundary staircase: measured against the previous held-scalar
    approach, seam steps were up to 36x larger than any step *within* a block.
    """
    omega = 2.0 * np.pi * freq_hz / sample_rate
    idx = np.arange(n_frames, dtype=np.float32)
    amp = np.linspace(amp_from, amp_to, n_frames, endpoint=False, dtype=np.float32)
    out = (np.sin(phase + omega * idx) * amp).astype(np.float32)
    return out, float((phase + omega * n_frames) % (2.0 * np.pi))


class _ToneEffect:
    """Shared gate/smooth/render for the single-sine effects.

    Subclasses compute a 0..1 target amplitude and a carrier frequency each
    block; this base owns amplitude smoothing, the release tail, and phase
    continuity. Keeping it in one place is deliberate — the release bug this
    replaces was duplicated identically across all four subclasses.
    """

    _tau_s: float = 0.05

    def __init__(self, sample_rate: int) -> None:
        self.sr = sample_rate
        self._phase = 0.0
        self._amp = 0.0
        self._freq_hz = 0.0

    def _step(self, n_frames: int, target: float, freq_hz: float) -> np.ndarray:
        # Latch the carrier while the effect is live so the release tail runs
        # at the last real frequency. Reading it fresh would let a gated-off
        # source (engine_rpm -> 0 on reset_features) hand us 0 Hz, turning the
        # tail into a DC pedestal — worse than the click it replaces.
        if target > 0.0:
            self._freq_hz = freq_hz

        prev = self._amp
        tau = self._tau_s if target > prev else _TAU_RELEASE_S
        alpha = _block_alpha(n_frames / self.sr, tau)
        self._amp = alpha * target + (1.0 - alpha) * prev

        if prev < _SILENCE and self._amp < _SILENCE:
            self._amp = 0.0
            return np.zeros(n_frames, dtype=np.float32)

        out, self._phase = _render_tone(
            self._phase, self._freq_hz, self.sr, n_frames, prev, self._amp
        )
        return out


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
    """FFT-based bandpass noise: white -> rfft -> zero out-of-band bins -> irfft.

    Normalized to a fixed RMS rather than a fixed peak, so bands of different
    widths carry equal energy and can be crossfaded without a level jump.
    """
    n = int(sample_rate * duration_s)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    spectrum = np.fft.rfft(noise)
    spectrum[(freqs < low_hz) | (freqs > high_hz)] = 0
    out = np.fft.irfft(spectrum, n).astype(np.float32)
    rms = float(np.sqrt(np.mean(out ** 2)))
    if rms > 0:
        out *= _VIB_BAND_RMS / rms
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
        self._amp = 0.0
        self._high_blend = 0.0

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
        target = 0.0
        if enabled:
            # Soft saturation, not a hard clip. Full scale is activity=0.005,
            # but a real kerb strike is ~0.05 — ten times over — so clipping
            # made kerbs, sausages, gravel and grass all read as an identical
            # 1.0 and deleted their magnitude before any user curve could
            # shape it. x/(1+x) keeps the small-signal slope and preserves
            # ordering far past where a clip has flattened.
            x = max(0.0, activity) * _VIB_ACTIVITY_SCALE
            normalized = x / (1.0 + x)
            target = apply_response_filter(
                normalized, input_gain, threshold, min_force, gamma
            ) * gain

        prev = self._amp
        tau = _TAU_VIBRATION_S if target > prev else _TAU_RELEASE_S
        alpha = _block_alpha(n_frames / self.sr, tau)
        self._amp = alpha * target + (1.0 - alpha) * prev

        # Speed → high-band blend factor (0..1), smoothed across callbacks.
        span = speed_blend_high_mps - speed_blend_low_mps
        if span <= 0:
            high_target = 0.0
        else:
            high_target = float(np.clip((speed_mps - speed_blend_low_mps) / span, 0.0, 1.0))
        prev_blend = self._high_blend
        blend_alpha = _block_alpha(n_frames / self.sr, _TAU_HIGH_BLEND_S)
        self._high_blend = blend_alpha * high_target + (1.0 - blend_alpha) * prev_blend

        # Wrap-read from both noise buffers at a shared cursor. The cursor
        # advances even while silent, so re-entry doesn't resume mid-waveform
        # at a stale sample.
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

        if prev < _SILENCE and self._amp < _SILENCE:
            self._amp = 0.0
            return np.zeros(n_frames, dtype=np.float32)

        # Crossfade the two equal-RMS bands rather than summing them: speed
        # should move the spectral centroid (measured 47 -> 70 Hz), not raise
        # the level. Both the blend and the amplitude ramp per sample.
        #
        # Equal-power (sqrt) weights, not linear. The two bands are independent
        # noise over disjoint spectra, so they sum in power, and linear weights
        # would cut RMS by 3 dB at the halfway point — a hole in the road feel
        # right in the speed range the blend exists to cover.
        blend = np.linspace(prev_blend, self._high_blend, n_frames, endpoint=False, dtype=np.float32)
        amp = np.linspace(prev, self._amp, n_frames, endpoint=False, dtype=np.float32)
        mix = low_chunk * np.sqrt(1.0 - blend) + high_chunk * np.sqrt(blend)
        return (mix * amp).astype(np.float32)


class EngineRumble(_ToneEffect):
    """Continuous-phase sine driven by engine RPM, amplitude-modulated by throttle.

    Felt vibration of a running engine, growing with throttle.
    """

    _tau_s = _TAU_ENGINE_S

    def process(
        self,
        n_frames: int,
        engine_rpm: float,
        throttle: int,
        gain: float,
        enabled: bool,
        rpm_divisor: float,
        min_rpm: float = 100.0,
        min_hz: float = 26.0,
        max_hz: float = 100.0,
    ) -> np.ndarray:
        target = 0.0
        freq_hz = self._freq_hz
        if enabled and engine_rpm >= min_rpm and rpm_divisor > 0:
            # Clamp into the band a shaker renders. Deliberately a clamp and
            # not an octave fold: a fold boundary gets crossed on every gear
            # pull, and halving the felt pitch while revs are still rising is
            # the percept of a gearchange — a false shift cue once per gear.
            freq_hz = min(max(engine_rpm / rpm_divisor, min_hz), max_hz)
            target = (throttle / 255.0) * gain
        return self._step(n_frames, target, freq_hz)


class BrakeRumble(_ToneEffect):
    """Low-frequency rumble whose amplitude scales with brake input above a threshold."""

    _tau_s = _TAU_BRAKE_S

    def process(
        self,
        n_frames: int,
        brake: int,
        gain: float,
        enabled: bool,
        freq_hz: float,
        threshold_pct: float,
    ) -> np.ndarray:
        target = 0.0
        brake_pct = brake / 255.0
        threshold = threshold_pct / 100.0
        if enabled and freq_hz > 0 and threshold < 1.0 and brake_pct > threshold:
            target = (brake_pct - threshold) / (1.0 - threshold) * gain
        return self._step(n_frames, target, freq_hz)


class RevLimiter(_ToneEffect):
    """Distinct buzz when engine RPM is at or above a configurable redline fraction."""

    _tau_s = _TAU_REV_LIMITER_S  # snappier so the limiter is felt right at threshold

    def process(
        self,
        n_frames: int,
        rpm_pct: float,
        gain: float,
        enabled: bool,
        freq_hz: float,
        trigger_pct: float,
    ) -> np.ndarray:
        target = 0.0
        trigger = trigger_pct / 100.0
        if enabled and freq_hz > 0 and trigger < 1.0 and rpm_pct > trigger:
            normalized = min(1.0, (rpm_pct - trigger) / (1.0 - trigger))
            target = normalized * gain
        return self._step(n_frames, target, freq_hz)


class WheelSlip(_ToneEffect):
    """Buzz triggered by wheelspin or lockup (any wheel's speed diverging from the car's)."""

    _tau_s = _TAU_SLIP_S

    def process(
        self,
        n_frames: int,
        slip_magnitude: float,
        gain: float,
        enabled: bool,
        freq_hz: float,
        threshold_mps: float,
        scale_mps: float,
    ) -> np.ndarray:
        target = 0.0
        if enabled and freq_hz > 0 and scale_mps > 0 and slip_magnitude > threshold_mps:
            normalized = min(1.0, (slip_magnitude - threshold_mps) / scale_mps)
            target = normalized * gain
        return self._step(n_frames, target, freq_hz)


class GearShift:
    """Short percussive thump retriggered when the bus' shift counter advances.

    Frequency, duration and gain are all captured at trigger time so that
    mid-thump changes don't cause a discontinuity in the envelope.
    """

    # A retrigger lands mid-thump on a close-ratio downshift. Fading the tail
    # out over this window instead of resetting the envelope avoids a step of
    # up to 0.845 at the longest UI-allowed duration.
    _RETRIGGER_FADE_S = 0.002

    def __init__(self, sample_rate: int) -> None:
        self.sr = sample_rate
        self._last_count = 0
        self._envelope_pos = -1
        self._envelope_len = 0
        self._freq_hz = 0.0
        self._gain = 0.0
        self._phase = 0.0
        # Tail of the thump being replaced by a retrigger, faded out under the
        # new one. Empty when no retrigger is in flight.
        self._fade: np.ndarray = np.zeros(0, dtype=np.float32)

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
            self._fade = np.zeros(0, dtype=np.float32)
            return out

        # `!=` rather than `>`: the bus counter is monotonic in practice, but a
        # restart resets it to 0 and a `>` comparison would then swallow every
        # shift until it climbed back past the stale high-water mark.
        if count != self._last_count:
            self._fade = self._retrigger_fade()
            self._envelope_pos = 0
            self._envelope_len = max(1, int(self.sr * duration_s))
            self._freq_hz = freq_hz
            # Latch the gain too. stream.py recomputes it from live RPM every
            # block, so an unlatched gain stepped ~3.3 dB mid-transient during
            # a downshift rev-match and made the same shift feel different run
            # to run, depending on which block sampled which RPM.
            self._gain = gain
            self._phase = 0.0
            self._last_count = count

        if self._envelope_pos >= 0:
            remaining = self._envelope_len - self._envelope_pos
            n = min(remaining, n_frames)
            if n <= 0:
                self._envelope_pos = -1
            else:
                idx = self._envelope_pos + np.arange(n, dtype=np.float32)
                env = (1.0 - idx / self._envelope_len) ** 2
                t = idx / self.sr
                out[:n] = (
                    env * np.sin(2.0 * np.pi * self._freq_hz * t)
                ).astype(np.float32) * self._gain
                self._envelope_pos += n
                if self._envelope_pos >= self._envelope_len:
                    self._envelope_pos = -1

        if self._fade.size:
            n = min(self._fade.size, n_frames)
            out[:n] += self._fade[:n]
            self._fade = self._fade[n:]
        return out

    def _retrigger_fade(self) -> np.ndarray:
        """Render the outgoing thump's next few ms under a linear fade-out."""
        if self._envelope_pos < 0:
            return np.zeros(0, dtype=np.float32)
        n = min(
            max(1, int(self.sr * self._RETRIGGER_FADE_S)),
            self._envelope_len - self._envelope_pos,
        )
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        idx = self._envelope_pos + np.arange(n, dtype=np.float32)
        env = (1.0 - idx / self._envelope_len) ** 2
        t = idx / self.sr
        tail = (env * np.sin(2.0 * np.pi * self._freq_hz * t)).astype(np.float32) * self._gain
        return tail * np.linspace(1.0, 0.0, n, endpoint=False, dtype=np.float32)
