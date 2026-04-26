"""Shared state between the asyncio telemetry side and the audio callback thread.

Atomic-ish: relies on CPython object-reference assignment being atomic, plus
small numeric updates that can tolerate brief inconsistency. No locks on the
audio path — callbacks must stay fast.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from shaker.config import AudioConfig
from shaker.gt7.protocol import TelemetryPacket

# Suspension feature smoothing window — ~0.5 s at 60 Hz telemetry rate.
_SUSP_WINDOW = 30
# Exponential smoothing factor for the activity envelope.
_ACTIVITY_ATTACK = 0.4
_ACTIVITY_DECAY = 0.92

# Test-burst activity level: roughly maxes out the modulation envelope.
_TEST_VIBRATION_ACTIVITY = 0.005


@dataclass
class TelemetryFeatures:
    """Audio-relevant signals derived from telemetry."""

    speed_mps: float = 0.0
    suspension_activity: float = 0.0  # smoothed RMS deviation, ~unitless
    engine_rpm: float = 0.0
    engine_rpm_pct: float = 0.0  # 0..1 relative to the car's max_alert_rpm
    lap_count: int = 0


class AudioBus:
    """Lockless shared state owned by the audio thread but written from asyncio."""

    def __init__(self, audio_config: AudioConfig) -> None:
        self.audio_config: AudioConfig = audio_config
        self.features: TelemetryFeatures = TelemetryFeatures()
        # Monotonically incremented when the gear changes upward in a sane range.
        self.gear_shift_count: int = 0

        # Internal — only touched from the asyncio side.
        self._suspension_history: deque[float] = deque(maxlen=_SUSP_WINDOW)
        self._last_gear: int | None = None

        # Test-mode override: monotonic deadline; while now() < this, the audio
        # thread sees a synthetic vibration activity. Lets the user verify
        # output without a live PS5.
        self._test_vibration_until: float = 0.0

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

    def push_packet(self, p: TelemetryPacket) -> None:
        """Update derived features and shift events from a new packet.

        Packets with `lap_count < 0` (menus, replays) are ignored — derived
        signals reset to silence and the gear-change tracker forgets state so
        a return to active play doesn't fire a spurious shift.
        """
        if p.lap_count < 0:
            self.features = TelemetryFeatures()
            self._suspension_history.clear()
            self._last_gear = None
            return

        avg_susp = 0.25 * (
            p.suspension_FL + p.suspension_FR + p.suspension_RL + p.suspension_RR
        )
        self._suspension_history.append(avg_susp)

        if len(self._suspension_history) >= 5:
            mean = sum(self._suspension_history) / len(self._suspension_history)
            deviation = abs(avg_susp - mean)
            prev = self.features.suspension_activity
            if deviation > prev:
                new = _ACTIVITY_ATTACK * deviation + (1 - _ACTIVITY_ATTACK) * prev
            else:
                new = _ACTIVITY_DECAY * prev
        else:
            new = 0.0

        rpm_pct = 0.0
        if p.max_alert_rpm > 0:
            rpm_pct = max(0.0, min(1.0, p.engine_rpm / p.max_alert_rpm))

        # Replace the dataclass atomically (single ref assignment).
        self.features = TelemetryFeatures(
            speed_mps=p.speed_mps,
            suspension_activity=new,
            engine_rpm=p.engine_rpm,
            engine_rpm_pct=rpm_pct,
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
