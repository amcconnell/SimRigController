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


def _alpha(block_s: float, tau_s: float) -> float:
    return 1.0 - math.exp(-block_s / tau_s)


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
        self._vibration = RoadVibration(self._sample_rate)
        self._gear_shift = GearShift(self._sample_rate)
        self._engine = EngineRumble(self._sample_rate)
        self._brake = BrakeRumble(self._sample_rate)
        self._rev_limiter = RevLimiter(self._sample_rate)
        self._slip = WheelSlip(self._sample_rate)
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

        try:
            self._stream = sd.OutputStream(
                samplerate=self._sample_rate,
                blocksize=self._block_size,
                channels=1,
                dtype="float32",
                device=device,
                callback=self._callback,
            )
        except Exception:
            log.exception("failed to open audio output; running silent")
            await self._stop.wait()
            return

        log.info(
            "audio out: device=%s sr=%d block=%d frames",
            device if device is not None else "default",
            self._sample_rate, self._block_size,
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

        cfg = self._bus.audio_config
        activity = self._bus.current_vibration_activity()
        rpm_factor = gear_shift_rpm_factor(
            self._bus.features.engine_rpm_pct,
            cfg.gear_shift_rpm_pct_low / 100.0,
            cfg.gear_shift_rpm_pct_high / 100.0,
            cfg.gear_shift_min_gain_pct / 100.0,
            cfg.gear_shift_max_gain_pct / 100.0,
        )

        vib = self._vibration.process(
            frames,
            activity,
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
        slip = self._slip.process(
            frames,
            self._bus.current_slip_magnitude(),
            cfg.wheel_slip_gain,
            cfg.wheel_slip_enabled,
            cfg.wheel_slip_freq_hz,
            cfg.wheel_slip_threshold_mps,
            cfg.wheel_slip_scale_mps,
        )

        block_s = frames / self._sample_rate

        # Mute is a ramped gain, not a buffer wipe — cutting mid-cycle is the
        # same broadband step the effects were just taught not to make. Once
        # the ramp has settled at zero we can hand back true silence.
        mute_target = 0.0 if self._bus.muted else 1.0
        mute_prev = self._mute_gain
        ma = _alpha(block_s, _MUTE_TAU_S)
        self._mute_gain = ma * mute_target + (1.0 - ma) * mute_prev
        if mute_prev < 1e-4 and self._mute_gain < 1e-4:
            self._mute_gain = 0.0
            outdata[:] = 0.0
            return

        mix = vib + gear + engine + brake + rev_limit + slip
        np.multiply(mix, cfg.master_gain, out=mix)

        # Fast attack, smoothed release. The gain comes from this block's own
        # peak, so the reduction must be in place near the top of the block —
        # spreading it across the whole block leaves the early samples at the
        # old gain, and that is exactly where a transient's peak sits.
        peak = float(np.max(np.abs(mix)))
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
        np.multiply(mix, gain, out=mix)
        # Net, not the primary mechanism: the limiter above cannot see past the
        # end of the block, so a within-block excursion can still just exceed 1.
        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:, 0] = mix
