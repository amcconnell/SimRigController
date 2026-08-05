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

# Exiting a session via GT7's pause menu clears the paused bit but leaves
# on_track set and the payload frozen at the last driving frame (measured
# 2026-07-06) — flags say "driving", so the flag gate passes and stale
# values shake the rig indefinitely. Real physics never repeats
# bit-identical floats for this many consecutive frames; a frozen payload
# means a menu. Half a second at 60 Hz.
_FREEZE_RESET_FRAMES = 30


def _activity_envelope(prev: float, bump: float) -> float:
    """One step of the asymmetric attack/decay envelope on a post-HPF bump level.

    Stateful *and* asymmetric, which is why every axle carries its own `prev`
    rather than being derived from the whole-car value. Attack/decay does not
    commute with max(): on alternating-corner input (a ripple strip hits FL,
    then RR, then FL...) the whole-car envelope attacks on every frame while
    each axle's envelope decays on the frames its own corners are quiet, so
    max(env_front, env_rear) != env(max(all four)).
    """
    if bump > prev:
        return _ACTIVITY_ATTACK * bump + (1 - _ACTIVITY_ATTACK) * prev
    return _ACTIVITY_DECAY * prev


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
    brake: int = 0  # 0..255
    # Worst-case wheel-vs-vehicle speed mismatch in m/s, across the four corners.
    # Spikes during wheelspin (under acceleration) or lockup (under braking).
    slip_magnitude: float = 0.0
    lap_count: int = 0

    # Body motion straight off the wire. Meaning UNVERIFIED — acceleration,
    # velocity and displacement are all plausible, and the reference frame is
    # unknown. Nothing audible reads these.
    sway: float = 0.0
    heave: float = 0.0
    surge: float = 0.0
    has_motion: bool = False

    # Independently derived references, computed purely so the fields above
    # can be identified by comparison on a live rig. long_accel is d(speed)/dt;
    # lat_accel is v * yaw_rate, exact only at zero sideslip but close enough
    # to tell an acceleration from a displacement.
    long_accel: float = 0.0   # m/s^2, + accelerating
    lat_accel: float = 0.0    # m/s^2

    # --- Per-axle split (front = pedal-deck shaker, rear = seat shaker) -------
    # Diagnostics only for now: nothing on the audio path reads these yet. They
    # exist so the per-corner mapping and units the two-channel stage depends on
    # can be checked against a real console instead of assumed.

    # Same units and treatment as `suspension_activity` above (max of the
    # high-passed corners on that axle, through the same asymmetric envelope),
    # but reduced over FL/FR and RL/RR instead of all four. Each carries its own
    # envelope state — see _activity_envelope for why they cannot be rebuilt
    # from the whole-car value.
    suspension_activity_front: float = 0.0
    suspension_activity_rear: float = 0.0

    # SIGNED wheel-vs-vehicle speed mismatch in m/s, per axle:
    #   positive => wheel surface is faster than the car (wheelspin)
    #   negative => wheel surface is slower than the car (lockup)
    # Reduced per axle by the corner with the largest |value|, keeping its sign.
    # `slip_magnitude` above throws that sign away, so it cannot tell a locked
    # wheel from a spinning one; these can.
    #
    # wheel_rps units and sign were settled on a live console 2026-08-05:
    # rad/s, sent negated by GT7, normalized on parse. Still UNVERIFIED: that
    # the FL/FR/RL/RR corner order matches the offsets, and whether speed_mps
    # is signed in reverse — if it is an unsigned magnitude, reversing reads as
    # total lockup. Exposed over /api/status so both can be settled by driving.
    slip_front: float = 0.0
    slip_rear: float = 0.0


class AudioBus:
    """Lockless shared state owned by the audio thread but written from asyncio."""

    def __init__(self, audio_config: AudioConfig) -> None:
        self.audio_config: AudioConfig = audio_config
        self.features: TelemetryFeatures = TelemetryFeatures()
        # Incremented on any change between two engaged gears — up or down.
        self.gear_shift_count: int = 0
        # Latest car identity. Deliberately not part of TelemetryFeatures and
        # not cleared by reset_features(): which car you are in does not stop
        # being true because the game paused, and clearing it would make the
        # rig re-derive its routing every time you open a menu.
        self.car_code: int | None = None
        # System-wide mute. In-memory only — doesn't persist across restarts
        # (intentional: "I muted to take a call" shouldn't pollute config).
        self.muted: bool = False

        # Internal — only touched from the asyncio side.
        self._hpf_FL = _OnePoleHPF()
        self._hpf_FR = _OnePoleHPF()
        self._hpf_RL = _OnePoleHPF()
        self._hpf_RR = _OnePoleHPF()
        # Per-axle envelope accumulators. Separate state, not a view of
        # `features`, because an envelope is only correct if it sees every
        # frame of its own axle — see _activity_envelope.
        self._activity_front: float = 0.0
        self._activity_rear: float = 0.0
        self._last_gear: int | None = None
        # Previous frame, for differentiating speed. packet_id is a better
        # clock than wall time: it increments once per physics frame, so a
        # dropped packet shows up as a gap rather than a phantom spike.
        self._prev_packet_id: int = 0
        self._prev_speed: float = 0.0
        self._long_accel: float = 0.0
        self._freeze_key: tuple | None = None
        self._freeze_count: int = 0

        # Test-mode override: monotonic deadline; while now() < this, the audio
        # thread sees a synthetic vibration activity. Lets the user verify
        # output without a live PS5.
        self._test_vibration_until: float = 0.0

        # Engine-sweep test state. While active, the audio thread reads
        # synthetic (rpm, throttle) instead of the feature values.
        self._test_engine_start: float = 0.0
        self._test_engine_duration: float = 0.0
        self._test_engine_peak_rpm: float = 0.0

        # Test overrides for brake, rev limiter, wheel slip. Each is a
        # triangular ramp (0 → peak → 0) over duration. While active, the
        # corresponding accessor returns the synthetic value.
        self._test_brake_start: float = 0.0
        self._test_brake_duration: float = 0.0
        self._test_brake_peak: int = 0

        self._test_rev_limiter_start: float = 0.0
        self._test_rev_limiter_duration: float = 0.0

        self._test_slip_start: float = 0.0
        self._test_slip_duration: float = 0.0
        self._test_slip_peak_mps: float = 0.0

        # Wiring check: a front-only pulse, a gap, then a rear-only pulse,
        # rendered by the audio thread in place of the whole mix. The only way
        # to prove the amp channels are not reversed — get that wrong and every
        # routing decision in the app is silently inverted.
        #
        # Only the *request* lives here. The schedule is advanced by the audio
        # thread in rendered frames rather than wall-clock time, so the pulse
        # is exactly as long as it sounds regardless of callback jitter.
        self.wiring_check_count: int = 0
        self.wiring_pulse_s: float = 1.0
        self.wiring_gap_s: float = 0.5

        # Post-pan output levels, written by the audio thread and read by the
        # web thread. Plain floats: torn reads are harmless for a meter, and a
        # lock on the audio path is not.
        self.meter_front: float = 0.0
        self.meter_rear: float = 0.0

    def update_audio_config(self, cfg: AudioConfig) -> None:
        self.audio_config = cfg

    def reset_features(self) -> None:
        """Clear all derived state — audio goes silent until packets resume.

        Called when telemetry stops arriving (GT7 exited or PS5 disconnected)
        so the last in-session values don't keep driving the shaker forever.
        Idempotent; safe to call repeatedly while telemetry is stale.
        """
        self.features = TelemetryFeatures()
        for f in (self._hpf_FL, self._hpf_FR, self._hpf_RL, self._hpf_RR):
            f.reset()
        self._activity_front = 0.0
        self._activity_rear = 0.0
        self._prev_packet_id = 0
        self._prev_speed = 0.0
        self._long_accel = 0.0
        self._last_gear = None

    def current_vibration_activity(self) -> float:
        """Activity value the audio thread should use this callback."""
        if time.monotonic() < self._test_vibration_until:
            return max(_TEST_VIBRATION_ACTIVITY, self.features.suspension_activity)
        return self.features.suspension_activity

    def current_vibration_activity_front(self) -> float:
        """Front-axle activity, with the same synthetic test burst applied.

        The test button has to reach both channels or it can't be used to set
        front/rear balance — which is most of what it is for on a two-shaker rig.
        """
        if time.monotonic() < self._test_vibration_until:
            return max(_TEST_VIBRATION_ACTIVITY, self.features.suspension_activity_front)
        return self.features.suspension_activity_front

    def current_vibration_activity_rear(self) -> float:
        if time.monotonic() < self._test_vibration_until:
            return max(_TEST_VIBRATION_ACTIVITY, self.features.suspension_activity_rear)
        return self.features.suspension_activity_rear

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

    def trigger_test_brake_rumble(
        self, duration_s: float = 2.0, peak_brake: int = 220
    ) -> None:
        self._test_brake_start = time.monotonic()
        self._test_brake_duration = duration_s
        self._test_brake_peak = peak_brake

    def current_brake(self) -> int:
        elapsed = time.monotonic() - self._test_brake_start
        if 0.0 <= elapsed < self._test_brake_duration:
            progress = elapsed / self._test_brake_duration
            envelope = 1.0 - abs(2.0 * progress - 1.0)
            return int(self._test_brake_peak * envelope)
        return self.features.brake

    def trigger_test_rev_limiter(self, duration_s: float = 2.0) -> None:
        self._test_rev_limiter_start = time.monotonic()
        self._test_rev_limiter_duration = duration_s

    def current_rpm_pct(self) -> float:
        elapsed = time.monotonic() - self._test_rev_limiter_start
        if 0.0 <= elapsed < self._test_rev_limiter_duration:
            progress = elapsed / self._test_rev_limiter_duration
            # Triangular ramp 0 → 1 → 0 carries rpm_pct past any reasonable trigger.
            return 1.0 - abs(2.0 * progress - 1.0)
        return self.features.engine_rpm_pct

    def trigger_test_wheel_slip(
        self, duration_s: float = 2.0, peak_slip_mps: float = 7.0
    ) -> None:
        self._test_slip_start = time.monotonic()
        self._test_slip_duration = duration_s
        self._test_slip_peak_mps = peak_slip_mps

    def current_slip_magnitude(self) -> float:
        elapsed = time.monotonic() - self._test_slip_start
        if 0.0 <= elapsed < self._test_slip_duration:
            progress = elapsed / self._test_slip_duration
            envelope = 1.0 - abs(2.0 * progress - 1.0)
            return self._test_slip_peak_mps * envelope
        return self.features.slip_magnitude

    def current_slip_front(self) -> float:
        """Signed front-axle slip. The test ramp drives this *negative* —
        a locked front is what braking into a corner actually produces."""
        elapsed = time.monotonic() - self._test_slip_start
        if 0.0 <= elapsed < self._test_slip_duration:
            progress = elapsed / self._test_slip_duration
            envelope = 1.0 - abs(2.0 * progress - 1.0)
            return -self._test_slip_peak_mps * envelope
        return self.features.slip_front

    def current_slip_rear(self) -> float:
        """Signed rear-axle slip. The test ramp drives this *positive* — rear
        wheelspin — so one button press demonstrates both voices and both
        channels."""
        elapsed = time.monotonic() - self._test_slip_start
        if 0.0 <= elapsed < self._test_slip_duration:
            progress = elapsed / self._test_slip_duration
            envelope = 1.0 - abs(2.0 * progress - 1.0)
            return self._test_slip_peak_mps * envelope
        return self.features.slip_rear

    def trigger_wiring_check(self, pulse_s: float = 1.0, gap_s: float = 0.5) -> None:
        self.wiring_pulse_s = pulse_s
        self.wiring_gap_s = gap_s
        self.wiring_check_count += 1

    def push_packet(self, p: TelemetryPacket) -> None:
        """Update derived features and shift events from a new packet.

        Packets are ignored — derived signals reset to silence and the
        gear-change tracker forgets state — when the player isn't driving:
        - `lap_count < 0`: menu / replay
        - `is_paused`: pause menu (flags bit 1)
        - not `is_on_track`: between sessions, pre-race waiting, etc. (flags bit 0)
        """
        if p.lap_count < 0 or is_paused(p) or not is_on_track(p):
            self.reset_features()
            return

        # Frozen-payload gate — see _FREEZE_RESET_FRAMES. A stationary car
        # false-positive is harmless: every effect is already silent at
        # zero throttle/brake/motion.
        key = (
            p.position_x, p.position_y, p.position_z,
            p.engine_rpm, p.speed_mps,
            p.suspension_FL, p.suspension_FR, p.suspension_RL, p.suspension_RR,
        )
        if key == self._freeze_key:
            self._freeze_count += 1
            if self._freeze_count >= _FREEZE_RESET_FRAMES:
                self.reset_features()
                return
        else:
            self._freeze_key = key
            self._freeze_count = 0

        self.car_code = p.car_code or None

        # Per-corner HPF — isolates bump transients from slow load shifts.
        fl = self._hpf_FL.step(p.suspension_FL)
        fr = self._hpf_FR.step(p.suspension_FR)
        rl = self._hpf_RL.step(p.suspension_RL)
        rr = self._hpf_RR.step(p.suspension_RR)
        # Per-axle maxima, for the pedal-deck and seat shakers respectively.
        bump_front = max(abs(fl), abs(fr))
        bump_rear = max(abs(rl), abs(rr))
        # Max across corners — preserves single-wheel hits the average would
        # smear. Taken over the pre-envelope bump levels, so it is the same
        # number the four-corner max has always produced; the *envelopes* below
        # are run independently and never combined back into this one.
        bump = max(bump_front, bump_rear)

        new = _activity_envelope(self.features.suspension_activity, bump)
        self._activity_front = _activity_envelope(self._activity_front, bump_front)
        self._activity_rear = _activity_envelope(self._activity_rear, bump_rear)

        rpm_pct = 0.0
        if p.max_alert_rpm > 0:
            rpm_pct = max(0.0, min(1.0, p.engine_rpm / p.max_alert_rpm))

        # Per-corner slip = wheel rim speed (rps × radius) − vehicle speed.
        # Signed: positive is the wheel outrunning the car (spin), negative is
        # the car outrunning the wheel (lockup).
        slip_FL = p.wheel_rps_FL * p.tire_radius_FL - p.speed_mps
        slip_FR = p.wheel_rps_FR * p.tire_radius_FR - p.speed_mps
        slip_RL = p.wheel_rps_RL * p.tire_radius_RL - p.speed_mps
        slip_RR = p.wheel_rps_RR * p.tire_radius_RR - p.speed_mps

        # Take the max absolute across corners as the headline magnitude.
        slip_magnitude = max(abs(slip_FL), abs(slip_FR), abs(slip_RL), abs(slip_RR))
        # Per axle: the corner that is misbehaving most, with its sign intact.
        # `key=abs` rather than abs() on the result — a locked front wheel and a
        # spinning one are opposite events that want opposite responses, and the
        # magnitude alone cannot tell them apart. Ties keep the left corner,
        # which only matters when the two are exact mirror images.
        slip_front = max((slip_FL, slip_FR), key=abs)
        slip_rear = max((slip_RL, slip_RR), key=abs)

        # Differentiate speed for a reference longitudinal acceleration.
        # Lightly smoothed: raw 60 Hz differentiation of a float is noisy
        # enough to obscure the comparison this exists for.
        frames = p.packet_id - self._prev_packet_id
        if self._prev_packet_id and 0 < frames <= 6:
            raw = (p.speed_mps - self._prev_speed) / (frames / _TELEMETRY_RATE_HZ)
            self._long_accel = 0.25 * raw + 0.75 * self._long_accel
        self._prev_packet_id = p.packet_id
        self._prev_speed = p.speed_mps

        # a_lat = v * yaw_rate; ang_vel_y is labelled yaw in protocol.py.
        lat_accel = p.speed_mps * p.ang_vel_y

        # Replace the dataclass atomically (single ref assignment).
        self.features = TelemetryFeatures(
            speed_mps=p.speed_mps,
            suspension_activity=new,
            engine_rpm=p.engine_rpm,
            engine_rpm_pct=rpm_pct,
            throttle=p.throttle,
            brake=p.brake,
            slip_magnitude=slip_magnitude,
            lap_count=p.lap_count,
            suspension_activity_front=self._activity_front,
            suspension_activity_rear=self._activity_rear,
            slip_front=slip_front,
            slip_rear=slip_rear,
            sway=p.sway,
            heave=p.heave,
            surge=p.surge,
            has_motion=p.has_motion,
            long_accel=self._long_accel,
            lat_accel=lat_accel,
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
