"""sounddevice output stream: mixes effect generators into one mono channel."""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from shaker.audio.bus import AudioBus
from shaker.audio.effects import GearShift, RoadVibration, gear_shift_rpm_factor

log = logging.getLogger(__name__)


class AudioOutput:
    """Owns the sounddevice OutputStream and the effect generators."""

    def __init__(self, bus: AudioBus) -> None:
        self._bus = bus
        cfg = bus.audio_config
        self._sample_rate = cfg.sample_rate
        self._block_size = max(32, int(cfg.sample_rate * cfg.buffer_ms / 1000))
        self._device = cfg.device if cfg.device != "default" else None
        self._vibration = RoadVibration(self._sample_rate)
        self._gear_shift = GearShift(self._sample_rate)
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
        )
        gear = self._gear_shift.process(
            frames,
            self._bus.gear_shift_count,
            cfg.gear_shift_gain * rpm_factor,
            cfg.gear_shift_enabled,
            cfg.gear_shift_freq_hz,
            cfg.gear_shift_duration_ms / 1000.0,
        )

        mix = vib + gear
        np.multiply(mix, cfg.master_gain, out=mix)
        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:, 0] = mix
