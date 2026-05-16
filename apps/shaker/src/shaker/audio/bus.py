"""Shared state between the asyncio telemetry side and the audio callback thread.

Atomic-ish: relies on CPython object-reference assignment being atomic, plus
small numeric updates that can tolerate brief inconsistency. No locks on the
audio path — callbacks must stay fast.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from shaker.config import AudioConfig
from shaker.gt7.protocol import TelemetryPacket, is_on_track, is_paused

# Per-corner suspension high-pass filter. Cutoff is chosen to sit above the
# body-motion / weight-transfer band (sub-Hz to a few Hz) but below the fastest
# packet rate. At 60 Hz telemetry, ~6 Hz HPF cleanly isolates bump transients
# from slow load shifts. We then take the max of |HPF_corner| across all four
# corners so single-wheel hits (kerbs, ripple strips) aren't averaged away.
_HPF_CUTOFF_HZ = 6.0
_TELEMETRY_RATE_HZ = 60.0
_HPF_ALPHA = math.exp(-2.0 * math.pi * _HPF_CUTOFF_HZ / _TELEMETRY_RATE_HZ)

# Asymmetric envelope on the post-HPF bump signal (fast attack, slow decay).
_ACTIVITY_ATTACK = 0.4
_ACTIVITY_DECAY = 0.92

# Test-burst activity level: roughly maxes out the modulation envelope.
_TEST_VIBRATION_ACTIVITY = 0.005


@dataclass
class _OnePoleHPF:
    """Single-pole high-pass. y[n] = a * (y[n-1] + x[n] - x[n-1])."""

    last_input: float = 0.0
    last_output: float = 0.0

    def step(self, x: float) -> float:
        y = _HPF_ALPHA * (self.last_output + x - self.last_input)
        self.last_input = x
        self.last_output = y
        return y

    def reset(self) -> None:
        self.last_input = 0.0
        self.last_output = 0.0


@dataclass
class TelemetryFeatures:
    """Audio-relevant signals derived from telemetry."""

    speed_mps: float = 0.0
    suspension_activity: float = 0.0  # smoothed RMS deviation, ~unitless
    engine_rpm: float = 0.0
    engine_rpm_pct: float = 0.0  # 0..1 relative to the car's max_alert_rpm
    throttle: int = 0  # 0..255
    lap_count: int = 0


class AudioBus:
    """Lockless shared state owned by the audio thread but written from asyncio."""

    def __init__(self, audio_config: AudioConfig) -> None:
        self.audio_config: AudioConfig = audio_config
        self.features: TelemetryFeatures = TelemetryFeatures()
        # Monotonically incremented when the gear changes upward in a sane range.
        self.gear_shift_count: int = 0

        # Internal — only touched from the asyncio side.
        self._hpf_FL = _OnePoleHPF()
        self._hpf_FR = _OnePoleHPF()
        self._hpf_RL = _OnePoleHPF()
        self._hpf_RR = _OnePoleHPF()
        self._last_gear: int | None = None

        # Test-mode override: monotonic deadline; while now() < this, the audio
        # thread sees a synthetic vibration activity. Lets the user verify
        # output without a live PS5.
        self._test_vibration_until: float = 0.0

        # Engine-sweep test state. While active, the audio thread reads
        # synthetic (rpm, throttle) instead of the feature values.
        self._test_engine_start: float = 0.0
        self._test_engine_duration: float = 0.0
        self._test_engine_peak_rpm: float = 0.0

    def update_audio_config(self, cfg: AudioConfig) -> None:
        self.audio_config = cfg

    def current_vibration_activity(self) -> float:
        """Activity value the audio thread should use this callback."""
        if time.monotonic() < self._test_vibration_until:
            return max(_TEST_VIBRATION_ACTIVITY, self.features.suspension_activity)
        return self.features.suspension_activity

    def trigger_test_vibration(self, duration_s: float = 1.0) -> None:
        self._test_vibration_until = time.monotonic() + duration_s

    def trigger_test_gear_shift(self) -> None:
        self.gear_shift_count += 1

    def trigger_test_engine_sweep(
        self, duration_s: float = 3.0, peak_rpm: float = 7000.0
    ) -> None:
        self._test_engine_start = time.monotonic()
        self._test_engine_duration = duration_s
        self._test_engine_peak_rpm = peak_rpm

    def current_engine_state(self) -> tuple[float, int]:
        """(engine_rpm, throttle) — synthetic during a sweep, otherwise the latest features."""
        elapsed = time.monotonic() - self._test_engine_start
        if 0.0 <= elapsed < self._test_engine_duration:
            progress = elapsed / self._test_engine_duration
            # Triangular ramp: 0 → 1 → 0.
            envelope = 1.0 - abs(2.0 * progress - 1.0)
            return (self._test_engine_peak_rpm * envelope, int(255 * envelope))
        return (self.features.engine_rpm, self.features.throttle)

    def push_packet(self, p: TelemetryPacket) -> None:
        """Update derived features and shift events from a new packet.

        Packets are ignored — derived signals reset to silence and the
        gear-change tracker forgets state — when the player isn't driving:
        - `lap_count < 0`: menu / replay
        - `is_paused`: pause menu (flags bit 1)
        - not `is_on_track`: between sessions, pre-race waiting, etc. (flags bit 0)
        """
        if p.lap_count < 0 or is_paused(p) or not is_on_track(p):
            self.features = TelemetryFeatures()
            for f in (self._hpf_FL, self._hpf_FR, self._hpf_RL, self._hpf_RR):
                f.reset()
            self._last_gear = None
            return

        # Per-corner HPF — isolates bump transients from slow load shifts.
        fl = self._hpf_FL.step(p.suspension_FL)
        fr = self._hpf_FR.step(p.suspension_FR)
        rl = self._hpf_RL.step(p.suspension_RL)
        rr = self._hpf_RR.step(p.suspension_RR)
        # Max across corners — preserves single-wheel hits the average would smear.
        bump = max(abs(fl), abs(fr), abs(rl), abs(rr))

        prev = self.features.suspension_activity
        if bump > prev:
            new = _ACTIVITY_ATTACK * bump + (1 - _ACTIVITY_ATTACK) * prev
        else:
            new = _ACTIVITY_DECAY * prev

        rpm_pct = 0.0
        if p.max_alert_rpm > 0:
            rpm_pct = max(0.0, min(1.0, p.engine_rpm / p.max_alert_rpm))

        # Replace the dataclass atomically (single ref assignment).
        self.features = TelemetryFeatures(
            speed_mps=p.speed_mps,
            suspension_activity=new,
            engine_rpm=p.engine_rpm,
            engine_rpm_pct=rpm_pct,
            throttle=p.throttle,
            lap_count=p.lap_count,
        )

        # Detect gear changes between any engaged gears (forward 1..8, reverse 15).
        # Transitions to/from 0 (neutral, paused, menu) are ignored to avoid
        # spurious thumps when the game pauses or the car comes to a stop in N.
        if self._last_gear is not None:
            if (
                p.current_gear != self._last_gear
                and self._last_gear > 0
                and p.current_gear > 0
            ):
                self.gear_shift_count += 1
        self._last_gear = p.current_gear
