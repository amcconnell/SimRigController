"""Two accelerometer pods, sampled on a background thread.

Front pod on the pedal deck, rear pod under the seat — the same two places the
shakers are, so what is measured is what the driver actually receives rather
than what the mixer was asked to send.

Everything here is written for an installation that does not exist yet and a
bus that may never appear. The app runs on a Mac during development, on a Pi
with nothing wired, and on a Pi mid-install with one pod connected and the
other in a hand. All three have to behave, so absence is a state rather than an
error, and pods are re-probed on a timer: start the app, plug a pod in, watch
it appear.

Sampling runs on its own thread, not the event loop. Draining two 32-deep FIFOs
is a few milliseconds of blocking I2C every poll, which is harmless on a thread
and would be jitter on the loop that also feeds the audio bus.

Gravity is tracked as a slow per-axis average and subtracted before any
vibration figure is computed. Without that, a pod's readings are dominated by a
constant 1 g that says nothing about what the rig is doing, and the number that
matters — the AC magnitude — would be buried under it.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from shaker.sensors.adxl345 import (
    ADDR_ALT,
    ADDR_PRIMARY,
    ADXL345,
    I2CBus,
    SensorError,
    open_bus,
)

log = logging.getLogger(__name__)

# How often the thread drains the FIFOs. The FIFO is 32 deep, so at 800 Hz it
# fills in 40 ms — polling at half that leaves margin for a late thread wake-up
# without dropping samples.
_POLL_S = 0.02

# Retry cadence for a pod that is absent or has faulted. Slow enough that an
# empty bus does not spin, fast enough to notice a pod being plugged in while
# you are standing at the rig with it.
_REPROBE_S = 2.0

# Gravity tracker. Long compared with anything a shaker does (the useful band
# starts at 20 Hz) and short enough to settle within a few seconds of the rig
# being tilted or a pod remounted.
_GRAVITY_TAU_S = 1.5

# Vibration RMS averaging. Short enough to respond while you tap the rig during
# installation, long enough not to flicker.
_RMS_TAU_S = 0.4

# Peak hold decay, so a single tap stays visible past the UI's 2 Hz poll.
_PEAK_TAU_S = 1.2

# Below this the pod is treated as sitting still. Roughly ten times the part's
# noise floor, so it reads a true zero at rest rather than a restless 0.003 g.
_QUIET_G = 0.01


def _alpha(dt_s: float, tau_s: float) -> float:
    return 1.0 - math.exp(-dt_s / tau_s)


@dataclass
class PodStats:
    """What one pod is currently reporting. Plain floats; read without a lock."""

    present: bool = False
    error: str | None = None
    # Raw latest reading, gravity included.
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    # Slow per-axis average — the gravity vector, and hence the orientation.
    gx: float = 0.0
    gy: float = 0.0
    gz: float = 0.0
    # AC magnitude with gravity removed. The number the rig is judged on.
    vibration_rms_g: float = 0.0
    vibration_peak_g: float = 0.0
    samples: int = 0
    rate_hz: float = 0.0

    @property
    def tilt_g(self) -> float:
        """Magnitude of the gravity vector. Should sit at 1.0 on a still rig."""
        return math.sqrt(self.gx * self.gx + self.gy * self.gy + self.gz * self.gz)

    def orientation(self) -> str:
        """Which way the pod is facing, from gravity alone.

        An installation aid: it says whether a pod ended up on its side without
        anyone having to reason about the silkscreen under a seat.
        """
        if not self.present or self.tilt_g < 0.5:
            return "unknown"
        axes = (("X", self.gx), ("Y", self.gy), ("Z", self.gz))
        name, value = max(axes, key=lambda a: abs(a[1]))
        return f"{name}{'+' if value > 0 else '-'}"


class Pod:
    """One sensor plus the running statistics kept for it."""

    def __init__(self, name: str, address: int, rate_hz: float, range_g: int) -> None:
        self.name = name
        self.address = address
        self._rate_hz = rate_hz
        self._range_g = range_g
        self._dev: ADXL345 | None = None
        self._last_probe = 0.0
        self._ms = 0.0                    # mean square of the AC component
        self._peak = 0.0
        self._count_since = 0
        self._count_at = 0.0
        # Integration window for a one-off measurement — see begin_window.
        self._win_sumsq = 0.0
        self._win_n = 0
        self._win_open = False
        self._primed = False
        self.stats = PodStats()

    def attach(self, bus: I2CBus | None) -> None:
        """(Re)connect if the part is there. Cheap enough to call on a timer."""
        now = time.monotonic()
        if bus is None or now - self._last_probe < _REPROBE_S:
            return
        self._last_probe = now
        dev = ADXL345(bus, self.address)
        if not dev.probe():
            if self.stats.present:
                log.warning("pod %s (0x%02x) disappeared", self.name, self.address)
            self._reset(present=False, error=None)
            return
        try:
            dev.configure(rate_hz=self._rate_hz, range_g=self._range_g)
        except SensorError as exc:
            self._reset(present=False, error=str(exc))
            return
        self._dev = dev
        self._reset(present=True, error=None)
        self.stats.rate_hz = 0.0
        log.info(
            "pod %s attached at 0x%02x (%.0f Hz, +/-%d g)",
            self.name, self.address, dev.rate_hz, dev.range_g,
        )

    def _reset(self, present: bool, error: str | None) -> None:
        if not present:
            self._dev = None
        self._primed = False
        self._ms = 0.0
        self._peak = 0.0
        self._count_since = 0
        self.stats = PodStats(present=present, error=error)

    def poll(self, dt_s: float) -> None:
        """Drain the FIFO and fold the samples into the running statistics."""
        if self._dev is None:
            return
        try:
            samples = self._dev.read_fifo()
        except SensorError as exc:
            log.warning("pod %s read failed: %s", self.name, exc)
            self._reset(present=False, error=str(exc))
            return
        if not samples:
            self._decay(dt_s)
            return

        s = self.stats
        # Per-sample time step. Using the true rate rather than the poll
        # interval keeps the filters correct when a poll returns a short or
        # long batch, which happens whenever the thread is scheduled late.
        rate = self._dev.rate_hz or self._rate_hz
        sdt = 1.0 / rate
        ga = _alpha(sdt, _GRAVITY_TAU_S)
        ra = _alpha(sdt, _RMS_TAU_S)

        gx, gy, gz = s.gx, s.gy, s.gz
        if not self._primed:
            # Seed the gravity tracker from the first reading instead of
            # letting it converge from zero. Starting at zero means the whole
            # 1 g of gravity is reported as vibration until the filter catches
            # up — about seven seconds at this time constant — so a pod plugged
            # in during installation would appear, show an enormous shake, and
            # slowly subside. The first sample is a far better estimate of
            # gravity than zero is, even if the rig is moving at that instant.
            first = samples[0]
            gx, gy, gz = first.x, first.y, first.z
            self._primed = True
        ms, peak = self._ms, self._peak
        # This batch only. Folded into the window below if one is open, so
        # the loop stays branch-free and nothing accumulates when it is not.
        batch_sumsq = 0.0
        for sample in samples:
            gx += ga * (sample.x - gx)
            gy += ga * (sample.y - gy)
            gz += ga * (sample.z - gz)
            ax, ay, az = sample.x - gx, sample.y - gy, sample.z - gz
            mag2 = ax * ax + ay * ay + az * az
            ms += ra * (mag2 - ms)
            mag = math.sqrt(mag2)
            if mag > peak:
                peak = mag
            batch_sumsq += mag2

        last = samples[-1]
        s.x, s.y, s.z = last.x, last.y, last.z
        s.gx, s.gy, s.gz = gx, gy, gz
        self._ms = ms
        if self._win_open:
            self._win_sumsq += batch_sumsq
            self._win_n += len(samples)
        rms = math.sqrt(max(ms, 0.0))
        s.vibration_rms_g = rms if rms > _QUIET_G else 0.0
        s.samples += len(samples)

        pa = _alpha(len(samples) * sdt, _PEAK_TAU_S)
        self._peak = peak * (1.0 - pa)
        s.vibration_peak_g = peak if peak > _QUIET_G else 0.0

        self._count_since += len(samples)
        now = time.monotonic()
        if self._count_at == 0.0:
            self._count_at = now
        elif now - self._count_at >= 1.0:
            s.rate_hz = self._count_since / (now - self._count_at)
            self._count_since = 0
            self._count_at = now

    def _decay(self, dt_s: float) -> None:
        """No samples this poll — let the held values fall rather than freeze."""
        pa = _alpha(dt_s, _PEAK_TAU_S)
        self._peak *= 1.0 - pa
        self.stats.vibration_peak_g = self._peak if self._peak > _QUIET_G else 0.0

    def begin_window(self) -> None:
        """Start integrating AC energy for a measurement.

        Deliberately lockless, like the rest of this module. A batch that
        straddles the boundary is counted on the wrong side, which at 20 ms
        batches against a window of about a second is under 0.2 dB — far below
        anything that would change a conclusion, and much cheaper than putting
        a lock on the sampling path.
        """
        self._win_sumsq = 0.0
        self._win_n = 0
        self._win_open = True

    def end_window(self) -> tuple[float, int]:
        """Close the window and return (rms of the AC magnitude, sample count).

        Closing matters: an accumulator left running would grow for as long as
        the process lives, and the boundary error argued for above only exists
        if there is a boundary.
        """
        self._win_open = False
        n, sumsq = self._win_n, self._win_sumsq
        # Consume it. A second read returning the previous window's data would
        # hand a caller a stale measurement that looks like a fresh one.
        self._win_n, self._win_sumsq = 0, 0.0
        if n <= 0:
            return (0.0, 0)
        return (math.sqrt(sumsq / n), n)

    def status(self) -> dict[str, Any]:
        s = self.stats
        return {
            "name": self.name,
            "address": f"0x{self.address:02x}",
            "present": s.present,
            "error": s.error,
            "x": round(s.x, 4),
            "y": round(s.y, 4),
            "z": round(s.z, 4),
            "tilt_g": round(s.tilt_g, 3),
            "orientation": s.orientation(),
            "vibration_rms_g": round(s.vibration_rms_g, 4),
            "vibration_peak_g": round(s.vibration_peak_g, 4),
            "samples": s.samples,
            "rate_hz": round(s.rate_hz, 1),
        }


@dataclass
class SensorHub:
    """Owns the bus, the pods, and the thread that reads them."""

    bus_number: int = 1
    front_address: int = ADDR_PRIMARY
    rear_address: int = ADDR_ALT
    rate_hz: float = 800.0
    range_g: int = 16
    enabled: bool = True

    _bus: I2CBus | None = field(default=None, init=False)
    _bus_error: str | None = field(default=None, init=False)
    _pods: list[Pod] = field(default_factory=list, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def __post_init__(self) -> None:
        self._pods = [
            Pod("front", self.front_address, self.rate_hz, self.range_g),
            Pod("rear", self.rear_address, self.rate_hz, self.range_g),
        ]

    def start(self, bus: I2CBus | None = None) -> None:
        """Open the bus and begin sampling. Never raises — absence is a state.

        A missing bus is the normal case on a development Mac and on a Pi
        before the pods are wired, so it is reported through `status()` for the
        UI to show rather than allowed to take the process down.
        """
        if not self.enabled:
            self._bus_error = "disabled in config"
            return
        if bus is not None:
            self._bus = bus
        else:
            try:
                self._bus = open_bus(self.bus_number)
            except SensorError as exc:
                self._bus_error = str(exc)
                log.info("accelerometer pods unavailable: %s", exc)
                return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sensors", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            for pod in self._pods:
                if pod._dev is None:
                    pod.attach(self._bus)
                else:
                    pod.poll(dt)
            self._stop.wait(_POLL_S)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def status(self) -> dict[str, Any]:
        pods = [p.status() for p in self._pods]
        return {
            "enabled": self.enabled,
            "bus": f"/dev/i2c-{self.bus_number}",
            "available": self._bus is not None,
            "error": self._bus_error,
            "any_present": any(p["present"] for p in pods),
            "pods": pods,
        }
