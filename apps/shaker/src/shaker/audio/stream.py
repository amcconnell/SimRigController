"""sounddevice output stream: mixes effect generators into one mono channel."""

from __future__ import annotations

import asyncio
import logging
import math

import numpy as np

from shaker.audio.bus import AudioBus
from shaker.audio.effects import (
    BrakeRumble,
    EngineRumble,
    GearShift,
    RevLimiter,
    RoadVibration,
    WheelSlip,
    gear_shift_rpm_factor,
)

log = logging.getLogger(__name__)

# Output limiter. Six effects at gain 1.0 sum well past unity — measured 2-27%
# of samples clipped at shipped defaults, and hard-clipping a 30-90 Hz mix
# folds intermodulation products into 120-300 Hz, a band the clean mix has
# essentially no energy in. Riding a smoothed gain instead keeps the artifacts
# out and preserves the relative level of whatever is playing.
_LIMIT_CEILING = 0.95
_LIMIT_RELEASE_S = 0.25
# Attack ramp length. Short enough that the reduction is in place before a
# transient's peak arrives, long enough that the gain change is not itself a
# step. Measured against the shipped defaults on a worst-case corner entry:
# ramping across the whole block still clips 0.103% of samples, dropping the
# gain instantly introduces a 0.227 seam, and this leaves 0.000% clipped with
# a seam of 0.0116 — identical to the largest ordinary step inside a block.
_LIMIT_ATTACK_S = 0.001
# Mute ramp. Short enough to feel instant, long enough not to be a step.
_MUTE_TAU_S = 0.015

# The rear road-vibration voice reads the shared noise loop from this far
# along. Circular autocorrelation of the 6 Hz-wide low band at this lag is
# ~1%, so the two channels are effectively independent without a second buffer.
_REAR_NOISE_OFFSET_S = 3.3

# Wiring check tone. Low enough to be unmistakably a shaker rather than a
# speaker, and 0.5 amplitude leaves headroom so it can bypass the limiter.
_WIRING_FREQ_HZ = 40.0
_WIRING_AMPLITUDE = 0.5
# Raised-cosine edges. 40 Hz is 0.8 cycles per 20 ms block, so a pulse
# boundary otherwise lands at an arbitrary phase and clicks.
_WIRING_EDGE_S = 0.02


def _alpha(block_s: float, tau_s: float) -> float:
    return 1.0 - math.exp(-block_s / tau_s)


def _pan(bias: float) -> tuple[float, float]:
    """Front/rear weights for a bias in -1 (all front) .. +1 (all rear).

    Linear and unity-sum, deliberately not constant-power. Both shakers receive
    the same waveform scaled by two constants, so the signals are perfectly
    correlated and sum in amplitude at the driver — constant-power laws are for
    decorrelated sources. Unity sum is also what lets the mono path be an exact
    passthrough rather than an approximation of the two halves.
    """
    b = min(1.0, max(-1.0, bias))
    return (0.5 * (1.0 - b), 0.5 * (1.0 + b))


class AudioOutput:
    """Owns the sounddevice OutputStream and the effect generators."""

    def __init__(self, bus: AudioBus) -> None:
        self._bus = bus
        cfg = bus.audio_config
        self._sample_rate = cfg.sample_rate
        self._block_size = max(32, int(cfg.sample_rate * cfg.buffer_ms / 1000))
        # Gain-riding state, carried across callbacks.
        self._limiter_gain = 1.0
        self._mute_gain = 1.0
        self._device = cfg.device if cfg.device != "default" else None
        # Fixed for the life of the stream — output_channels is restart-required.
        self._out_channels = 2 if cfg.output_channels == 2 else 1
        self._vibration = RoadVibration(self._sample_rate)
        self._gear_shift = GearShift(self._sample_rate)
        self._engine = EngineRumble(self._sample_rate)
        self._brake = BrakeRumble(self._sample_rate)
        self._rev_limiter = RevLimiter(self._sample_rate)
        self._slip = WheelSlip(self._sample_rate)
        # Second set of voices, only for a two-channel rig. Road vibration and
        # slip are the two effects with genuine per-axle telemetry behind them,
        # so they get real independent voices rather than a panned copy.
        self._vibration_rear: RoadVibration | None = None
        self._slip_rear: WheelSlip | None = None
        if self._out_channels == 2:
            self._vibration_rear = RoadVibration(
                self._sample_rate, cursor_offset_s=_REAR_NOISE_OFFSET_S
            )
            self._slip_rear = WheelSlip(self._sample_rate)
        # Wiring-check schedule, advanced in rendered frames rather than wall
        # clock so the pulse is exactly as long as it sounds. -1 means idle.
        self._wiring_phase = 0.0
        self._wiring_seen = 0
        self._wiring_frames = -1
        self._stop = asyncio.Event()
        self._stream = None  # type: ignore[assignment]

    async def run(self) -> None:
        try:
            import sounddevice as sd
        except OSError as exc:
            log.error("sounddevice unavailable (PortAudio missing?): %s", exc)
            await self._stop.wait()
            return

        # PortAudio caches the device list at module init. In-process restart
        # would otherwise miss devices plugged in after the first start.
        sd._terminate()
        sd._initialize()

        device = self._device
        if device is not None:
            try:
                sd.query_devices(device, "output")
            except ValueError:
                names = [
                    d["name"] for d in sd.query_devices()
                    if d["max_output_channels"] > 0
                ]
                log.warning(
                    "configured audio device %r not found; falling back to default. "
                    "available: %s",
                    device, names,
                )
                device = None

        # Try the configured channel count, then fall back to mono. Only a
        # PortAudio error is worth retrying — anything else is a real failure
        # and the blanket handler below leaves the app running silent, which
        # would turn a working single-shaker rig into a dead one just because
        # someone asked for a second channel the device cannot provide.
        for channels in (self._out_channels, 1):
            try:
                self._stream = sd.OutputStream(
                    samplerate=self._sample_rate,
                    blocksize=self._block_size,
                    channels=channels,
                    dtype="float32",
                    device=device,
                    callback=self._callback,
                )
            except sd.PortAudioError as exc:
                if channels == 1:
                    log.exception("failed to open audio output; running silent")
                    await self._stop.wait()
                    return
                log.warning(
                    "device %s refused %d channels (%s); falling back to mono",
                    device if device is not None else "default", channels, exc,
                )
                continue
            except Exception:
                log.exception("failed to open audio output; running silent")
                await self._stop.wait()
                return
            self._out_channels = channels
            break

        log.info(
            "audio out: device=%s sr=%d block=%d frames channels=%d (%s)",
            device if device is not None else "default",
            self._sample_rate, self._block_size, self._out_channels,
            "front/rear" if self._out_channels == 2 else "mono",
        )
        with self._stream:
            await self._stop.wait()
            self._stream.stop()

    def stop(self) -> None:
        self._stop.set()

    def _callback(self, outdata, frames: int, time, status) -> None:  # type: ignore[no-untyped-def]
        if status:
            # Underruns / overruns — log at debug to avoid spam.
            log.debug("audio status: %s", status)

        # The wiring check replaces the entire mix so the pulse is provably the
        # only thing playing. Without that, "did the rear shaker fire?" is
        # answered against a background of road noise.
        if self._bus.wiring_check_count != self._wiring_seen:
            self._wiring_seen = self._bus.wiring_check_count
            self._wiring_frames = 0
            self._wiring_phase = 0.0
        if self._wiring_frames >= 0 and self._render_wiring(outdata, frames):
            return

        cfg = self._bus.audio_config
        stereo = self._out_channels == 2
        rpm_factor = gear_shift_rpm_factor(
            self._bus.features.engine_rpm_pct,
            cfg.gear_shift_rpm_pct_low / 100.0,
            cfg.gear_shift_rpm_pct_high / 100.0,
            cfg.gear_shift_min_gain_pct / 100.0,
            cfg.gear_shift_max_gain_pct / 100.0,
        )

        # In stereo the front voice is driven by front-axle suspension only;
        # in mono it keeps reading the whole-car scalar, unchanged.
        vib = self._vibration.process(
            frames,
            self._bus.current_vibration_activity_front() if stereo
            else self._bus.current_vibration_activity(),
            cfg.vibration_gain,
            cfg.vibration_enabled,
            cfg.vibration_input_gain_pct / 100.0,
            cfg.vibration_threshold_pct / 100.0,
            cfg.vibration_min_force_pct / 100.0,
            cfg.vibration_gamma,
            self._bus.features.speed_mps,
            cfg.vibration_speed_blend_low_mps,
            cfg.vibration_speed_blend_high_mps,
        )
        gear = self._gear_shift.process(
            frames,
            self._bus.gear_shift_count,
            cfg.gear_shift_gain * rpm_factor,
            cfg.gear_shift_enabled,
            cfg.gear_shift_freq_hz,
            cfg.gear_shift_duration_ms / 1000.0,
        )
        engine_rpm, throttle = self._bus.current_engine_state()
        engine = self._engine.process(
            frames,
            engine_rpm,
            throttle,
            cfg.engine_rumble_gain,
            cfg.engine_rumble_enabled,
            cfg.engine_rumble_rpm_divisor,
            min_hz=cfg.engine_rumble_min_hz,
            max_hz=cfg.engine_rumble_max_hz,
        )
        brake = self._brake.process(
            frames,
            self._bus.current_brake(),
            cfg.brake_rumble_gain,
            cfg.brake_rumble_enabled,
            cfg.brake_rumble_freq_hz,
            cfg.brake_rumble_threshold_pct,
        )
        rev_limit = self._rev_limiter.process(
            frames,
            self._bus.current_rpm_pct(),
            cfg.rev_limiter_gain,
            cfg.rev_limiter_enabled,
            cfg.rev_limiter_freq_hz,
            cfg.rev_limiter_trigger_pct,
        )
        if stereo:
            # Per-axle and signed: a locked front and a spinning rear are
            # opposite corrections, each landing on the shaker nearest the axle
            # it describes. This is the payoff of the whole exercise.
            slip = self._slip.process(
                frames,
                self._bus.current_slip_front(),
                cfg.wheel_slip_gain,
                cfg.wheel_slip_enabled,
                cfg.wheel_slip_freq_hz,
                cfg.wheel_slip_threshold_mps,
                cfg.wheel_slip_scale_mps,
                cfg.wheel_slip_lock_freq_hz,
            )
        else:
            slip = self._slip.process(
                frames,
                self._bus.current_slip_magnitude(),
                cfg.wheel_slip_gain,
                cfg.wheel_slip_enabled,
                cfg.wheel_slip_freq_hz,
                cfg.wheel_slip_threshold_mps,
                cfg.wheel_slip_scale_mps,
            )

        if not stereo:
            # Bit-identical to the single-channel build. Deliberately not
            # reconstructed as front + rear: the pan weights sum to one in
            # exact arithmetic but not in float32, and the accumulation order
            # differs. A one-field rollback should be provably a no-op.
            mix = vib + gear + engine + brake + rev_limit + slip
            np.multiply(mix, cfg.master_gain, out=mix)
            self._write_channels(outdata, frames, (mix,))
            return

        rear_vib = self._vibration_rear.process(  # type: ignore[union-attr]
            frames,
            self._bus.current_vibration_activity_rear(),
            cfg.vibration_gain,
            cfg.vibration_enabled,
            cfg.vibration_input_gain_pct / 100.0,
            cfg.vibration_threshold_pct / 100.0,
            cfg.vibration_min_force_pct / 100.0,
            cfg.vibration_gamma,
            self._bus.features.speed_mps,
            cfg.vibration_speed_blend_low_mps,
            cfg.vibration_speed_blend_high_mps,
        )
        rear_slip = self._slip_rear.process(  # type: ignore[union-attr]
            frames,
            self._bus.current_slip_rear(),
            cfg.wheel_slip_gain,
            cfg.wheel_slip_enabled,
            cfg.wheel_slip_freq_hz,
            cfg.wheel_slip_threshold_mps,
            cfg.wheel_slip_scale_mps,
            cfg.wheel_slip_lock_freq_hz,
        )

        # Road and slip arrive already split by axle. The rest are chassis-wide
        # signals with no per-axle telemetry behind them, so they are placed by
        # a bias rather than derived — see the config comments for which are
        # physics and which are taste.
        front = vib + slip
        rear = rear_vib + rear_slip
        for signal, bias in (
            (gear, cfg.gear_shift_bias),
            (engine, cfg.engine_rumble_bias),
            (brake, cfg.brake_rumble_bias),
            (rev_limit, cfg.rev_limiter_bias),
        ):
            front_w, rear_w = _pan(bias)
            front += signal * front_w
            rear += signal * rear_w

        np.multiply(front, cfg.master_gain, out=front)
        np.multiply(rear, cfg.master_gain * cfg.rear_gain_trim, out=rear)
        self._write_channels(outdata, frames, (front, rear))

    def _write_channels(self, outdata, frames: int, bufs) -> None:  # type: ignore[no-untyped-def]
        """Mute ramp, shared-gain limiting, and the write into PortAudio.

        The limiter derives one gain from the loudest channel and applies it to
        every channel. Limiting each independently would move the front/rear
        balance under load — measured up to 5 dB rearward at high master gain,
        i.e. the front goes quiet exactly when the driver most needs to feel it
        loading up. Shared gain holds the ratio to 0.00 dB at every gain.
        """
        block_s = frames / self._sample_rate

        mute_target = 0.0 if self._bus.muted else 1.0
        mute_prev = self._mute_gain
        ma = _alpha(block_s, _MUTE_TAU_S)
        self._mute_gain = ma * mute_target + (1.0 - ma) * mute_prev
        if mute_prev < 1e-4 and self._mute_gain < 1e-4:
            self._mute_gain = 0.0
            # Every column, not just column 0 — PortAudio hands back a view on
            # its own buffer and never clears it, so an untouched second column
            # would replay its last block forever while the UI says "muted".
            outdata[:] = 0.0
            self._bus.meter_front = 0.0
            self._bus.meter_rear = 0.0
            return

        peak = max(float(np.max(np.abs(b))) for b in bufs)
        limit_target = _LIMIT_CEILING / peak if peak > _LIMIT_CEILING else 1.0
        limit_prev = self._limiter_gain
        if limit_target < limit_prev:
            self._limiter_gain = limit_target
            attack = min(int(self._sample_rate * _LIMIT_ATTACK_S), frames)
            gain = np.full(frames, limit_target, dtype=np.float32)
            if attack > 0:
                gain[:attack] = np.linspace(
                    limit_prev, limit_target, attack, endpoint=False, dtype=np.float32
                )
        else:
            la = _alpha(block_s, _LIMIT_RELEASE_S)
            self._limiter_gain = la * limit_target + (1.0 - la) * limit_prev
            gain = np.linspace(
                limit_prev, self._limiter_gain, frames, endpoint=False, dtype=np.float32
            )

        if mute_prev != 1.0 or self._mute_gain != 1.0:
            np.multiply(
                gain,
                np.linspace(mute_prev, self._mute_gain, frames, endpoint=False, dtype=np.float32),
                out=gain,
            )

        for i, buf in enumerate(bufs):
            np.multiply(buf, gain, out=buf)
            # Net, not the primary mechanism: the limiter cannot see past the
            # end of the block, so a within-block excursion can still just
            # exceed 1.
            np.clip(buf, -1.0, 1.0, out=buf)
            outdata[:, i] = buf

        self._bus.meter_front = float(np.max(np.abs(bufs[0])))
        self._bus.meter_rear = float(np.max(np.abs(bufs[1]))) if len(bufs) > 1 else 0.0

    def _render_wiring(self, outdata, frames: int) -> bool:  # type: ignore[no-untyped-def]
        """One channel at a time, bypassing the mix entirely.

        Answers the one question no amount of software can: are the amp
        channels the way round the app assumes? Get it wrong and every routing
        decision is silently inverted, which reads as "this feels subtly off"
        for weeks rather than as a bug.

        Returns False once the schedule is spent, so the caller falls through
        to the normal mix for the rest of that block.
        """
        pulse_s = max(self._bus.wiring_pulse_s, 1e-3)
        gap_s = max(self._bus.wiring_gap_s, 0.0)
        elapsed = self._wiring_frames / self._sample_rate
        rear_start = pulse_s + gap_s

        if elapsed < pulse_s:
            column, progress = 0, elapsed / pulse_s
        elif elapsed < rear_start:
            column, progress = -1, 0.0  # silent gap between the two pulses
        elif elapsed < rear_start + pulse_s:
            column, progress = 1, (elapsed - rear_start) / pulse_s
        else:
            self._wiring_frames = -1
            return False

        self._wiring_frames += frames
        outdata[:] = 0.0
        self._bus.meter_front = 0.0
        self._bus.meter_rear = 0.0
        if column < 0 or self._bus.muted:
            return True

        idx = np.arange(frames, dtype=np.float32)
        omega = 2.0 * np.pi * _WIRING_FREQ_HZ / self._sample_rate
        tone = np.sin(self._wiring_phase + omega * idx).astype(np.float32)
        self._wiring_phase = float((self._wiring_phase + omega * frames) % (2.0 * np.pi))

        # Raised-cosine fade at each end. 40 Hz is under one cycle per block,
        # so a hard pulse boundary lands at an arbitrary phase and clicks.
        edge = min(0.5, _WIRING_EDGE_S / pulse_s)
        pos = progress + (idx / self._sample_rate) / pulse_s
        env = np.ones(frames, dtype=np.float32)
        if edge > 0:
            ramp = np.minimum(np.clip(pos / edge, 0.0, 1.0),
                              np.clip((1.0 - pos) / edge, 0.0, 1.0))
            env = (0.5 - 0.5 * np.cos(np.pi * ramp)).astype(np.float32)

        signal = tone * env * _WIRING_AMPLITUDE
        # A mono stream still gets the front pulse; the rear pulse is silence,
        # which is itself the correct answer on a one-channel rig.
        if column < outdata.shape[1]:
            outdata[:, column] = signal
            if column == 0:
                self._bus.meter_front = float(np.max(np.abs(signal)))
            else:
                self._bus.meter_rear = float(np.max(np.abs(signal)))
        return True
