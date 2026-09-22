"""Swept-frequency response of the rig, measured at both pods.

The crosstalk measurement reports one number at one frequency. That was enough
to show the seat isolation mounts were doing something, and not enough to say
what: a single tone at 40 Hz cannot distinguish a mount that is working from a
mount whose resonance happens to sit just below the test frequency, and it
certainly cannot say where the good and bad frequencies are. Worse, a structure
whose modes move — which is what happens when a hundred kilos of seat and
driver are decoupled from a frame — can change a single-point reading by 20
percent without anything having got better or worse. It just moved.

So this plays one tone at a time across the band and reads both pods at each
step. What comes back is three things worth having:

  * **Transmissibility against frequency.** An isolator amplifies below its
    natural frequency, peaks at it, and only isolates above about 1.4 times it.
    The peak in this curve *is* the natural frequency, read directly instead of
    inferred from one point and an assumption.
  * **Where the isolation is good.** The useful output bands can then be put
    where the rig keeps the two channels apart, which costs nothing.
  * **Where the rig rings.** A peak in a pod's response to its own shaker is a
    structural mode — the frequencies at which a panel is doing the talking.

Drive amplitude is constant across the sweep rather than compensated for what
the shaker can deliver. Flattening it would hide the peaks and dips this exists
to find. Expect the bottom of the band to come back quiet and near the noise
floor; the result marks those points rather than pretending to a figure.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from shaker.sensors.tone import detect

log = logging.getLogger(__name__)

# The band, in sixth-octave steps. The bottom is below anything a bass shaker
# reproduces usefully, deliberately: the mount resonance is expected down there
# and it has to be inside the sweep to be seen. The top is above the useful
# output band, so the rolloff can be seen to continue rather than being cut off
# at the last measured point.
LOW_HZ = 15.0
HIGH_HZ = 120.0
STEPS_PER_OCTAVE = 6

# Per point: let the shaker and the structure reach steady state, then
# integrate. The settle is generous because the cost is only time, and a
# measurement taken during the build-up reads low in a way that looks exactly
# like good isolation.
SETTLE_S = 0.35
WINDOW_S = 0.8
GAP_S = 0.15

# Ambient is measured through the same detector and the same window length as
# the signal, because the detector's bandwidth depends on the window: a floor
# measured over a longer window would be narrower-band, hence lower, and every
# signal-to-noise figure derived from it would flatter the result.
AMBIENT_REPEATS = 3

# Drive level, matching the wiring check so the two measurements are directly
# comparable.
AMPLITUDE = 0.5

# A driven pod must beat its own ambient by this much for the point to count.
_MIN_SNR = 3.0

# Below this the far pod is not measuring transmission, it is measuring the
# room, and the true ratio is somewhere below what is reported.
_FLOOR_SNR = 2.0


def frequencies(
    low_hz: float = LOW_HZ,
    high_hz: float = HIGH_HZ,
    per_octave: int = STEPS_PER_OCTAVE,
) -> list[float]:
    """Geometric steps, because structures behave geometrically in frequency.

    Linear spacing would spend most of its points above 60 Hz, where little
    changes, and skate over the octave where the mount resonance lives.
    """
    if low_hz <= 0 or high_hz <= low_hz or per_octave < 1:
        return [max(low_hz, 1.0)]
    count = int(round(math.log2(high_hz / low_hz) * per_octave))
    return [round(low_hz * 2.0 ** (i / per_octave), 2) for i in range(count + 1)]


def _db(ratio: float) -> float:
    return 20.0 * math.log10(max(ratio, 1e-6))


@dataclass
class SweepPoint:
    """One tone, on one channel, as seen by both pods."""

    freq_hz: float
    drive: str                      # which channel played
    near_g: float = 0.0             # the pod at the driven end
    far_g: float = 0.0              # the pod at the other end
    ambient_near_g: float = 0.0
    ambient_far_g: float = 0.0
    found_hz: float = 0.0           # where the detector actually found the tone
    ratio_db: float | None = None
    ok: bool = False
    floor_limited: bool = False     # far pod at the noise floor; ratio is a bound
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "freq_hz": self.freq_hz,
            "drive": self.drive,
            "near_g": round(self.near_g, 5),
            "far_g": round(self.far_g, 5),
            "ambient_near_g": round(self.ambient_near_g, 5),
            "ambient_far_g": round(self.ambient_far_g, 5),
            "found_hz": round(self.found_hz, 2),
            "ratio_db": None if self.ratio_db is None else round(self.ratio_db, 1),
            "ok": self.ok,
            "floor_limited": self.floor_limited,
            "note": self.note,
        }


@dataclass
class SweepResult:
    points: list[SweepPoint] = field(default_factory=list)
    ok: bool = False
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "warnings": self.warnings,
            "summary": self.summary,
            "points": [p.as_dict() for p in self.points],
        }


def isolation_curve(points: list[SweepPoint]) -> list[dict[str, Any]]:
    """Pair the two drive directions at each frequency into one figure.

    Same mean-of-both-directions as the crosstalk measurement, and for the same
    reason: a per-pod sensitivity error enters the two directions as
    reciprocals, so it cancels exactly in the mean however badly the two pods
    are matched. That property is worth more here than in a single-point
    measurement, because a sweep is long enough for the pods to warm up and
    drift apart during it.
    """
    by_freq: dict[float, dict[str, SweepPoint]] = {}
    for p in points:
        by_freq.setdefault(p.freq_hz, {})[p.drive] = p

    curve = []
    for freq in sorted(by_freq):
        pair = by_freq[freq]
        front, rear = pair.get("front"), pair.get("rear")
        entry: dict[str, Any] = {
            "freq_hz": freq,
            "front_to_rear_db": None,
            "rear_to_front_db": None,
            "isolation_db": None,
            "floor_limited": False,
            "front_near_g": round(front.near_g, 5) if front else None,
            "rear_near_g": round(rear.near_g, 5) if rear else None,
        }
        if front and front.ok and front.ratio_db is not None:
            entry["front_to_rear_db"] = round(front.ratio_db, 1)
        if rear and rear.ok and rear.ratio_db is not None:
            entry["rear_to_front_db"] = round(rear.ratio_db, 1)
        if entry["front_to_rear_db"] is not None and entry["rear_to_front_db"] is not None:
            entry["isolation_db"] = round(
                0.5 * (entry["front_to_rear_db"] + entry["rear_to_front_db"]), 1
            )
            entry["floor_limited"] = bool(
                (front and front.floor_limited) or (rear and rear.floor_limited)
            )
        curve.append(entry)
    return curve


def summarise(points: list[SweepPoint], curve: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull the few numbers worth acting on out of the curve.

    This reports the best and worst frequencies and how far apart they are, and
    deliberately stops there. An earlier version called the worst frequency the
    mounts' natural frequency, on the reasoning that a mount's transmissibility
    peaks at resonance. The first real sweep showed why that was wrong: this rig
    does not produce an isolator curve at all. It swings 15 dB between adjacent
    sixth-octave points, structural modes dominate everywhere above 50 Hz, and
    the worst point landed at 75.6 Hz — which is a panel doing something, not
    four rubber mounts. Naming it fn would have dressed an artifact up as a
    measured property of the hardware.

    The spread is the number that turned out to matter. A rig whose isolation
    varies by 15 dB across the band is one where the choice of output frequency
    is worth more than any amount of mechanical work.
    """
    usable = [c for c in curve if c["isolation_db"] is not None and not c["floor_limited"]]
    out: dict[str, Any] = {
        "measured_points": len(points),
        "usable_points": len(usable),
    }
    if usable:
        worst = max(usable, key=lambda c: c["isolation_db"])
        best = min(usable, key=lambda c: c["isolation_db"])
        out["worst_hz"] = worst["freq_hz"]
        out["worst_isolation_db"] = worst["isolation_db"]
        out["best_hz"] = best["freq_hz"]
        out["best_isolation_db"] = best["isolation_db"]
        out["spread_db"] = round(worst["isolation_db"] - best["isolation_db"], 1)

    # Where each end rings loudest when driven by its own shaker. These are
    # structural modes, and they are what makes one frequency feel boomy.
    for drive in ("front", "rear"):
        driven = [p for p in points if p.drive == drive and p.ok]
        if driven:
            loud = max(driven, key=lambda p: p.near_g)
            out[f"{drive}_peak_hz"] = loud.freq_hz
            out[f"{drive}_peak_g"] = round(loud.near_g, 4)
    return out


def band_isolation(curve: list[dict[str, Any]], lo_hz: float, hi_hz: float) -> float | None:
    """Mean isolation across a band, for judging where the output bands sit."""
    vals = [
        c["isolation_db"] for c in curve
        if c["isolation_db"] is not None and lo_hz <= c["freq_hz"] <= hi_hz
    ]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


async def run(
    bus: Any,
    hub: Any,
    freqs: list[float] | None = None,
    settle_s: float = SETTLE_S,
    window_s: float = WINDOW_S,
    gap_s: float = GAP_S,
    amplitude: float = AMPLITUDE,
    ambient_repeats: int = AMBIENT_REPEATS,
    progress: Callable[[int, int], None] | None = None,
) -> SweepResult:
    """Play the sweep and measure it. Timings are parameters so tests are fast."""
    result = SweepResult()
    pods = {p.name: p for p in hub._pods}
    front, rear = pods.get("front"), pods.get("rear")
    if front is None or rear is None:
        result.reason = "both pods are required"
        return result
    missing = [n for n, p in (("front", front), ("rear", rear)) if not p.stats.present]
    if missing:
        result.reason = f"not detected: {', '.join(missing)}"
        return result

    tones = freqs if freqs is not None else frequencies()
    total = len(tones) * 2 + ambient_repeats
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    async def capture(seconds: float) -> tuple[tuple, tuple]:
        front.begin_capture()
        rear.begin_capture()
        await asyncio.sleep(seconds)
        return (front.end_capture(), rear.end_capture())

    # Ambient first, through the same detector and window length as the signal.
    ambient_power: dict[float, list[float]] = {f: [0.0, 0.0] for f in tones}
    for _ in range(max(1, ambient_repeats)):
        fcap, rcap = await capture(window_s)
        for f in tones:
            fa, _ = detect(fcap[0], fcap[1], fcap[2], fcap[3], f)
            ra, _ = detect(rcap[0], rcap[1], rcap[2], rcap[3], f)
            ambient_power[f][0] += fa * fa
            ambient_power[f][1] += ra * ra
        tick()
        await asyncio.sleep(gap_s)

    n = max(1, ambient_repeats)
    ambient = {
        f: (math.sqrt(p[0] / n), math.sqrt(p[1] / n)) for f, p in ambient_power.items()
    }

    for drive, channel in (("front", 0), ("rear", 1)):
        for freq in tones:
            bus.trigger_tone(freq, channel, settle_s + window_s + 0.1, amplitude)
            await asyncio.sleep(settle_s)
            fcap, rcap = await capture(window_s)

            f_amp, f_at = detect(fcap[0], fcap[1], fcap[2], fcap[3], freq)
            r_amp, r_at = detect(rcap[0], rcap[1], rcap[2], rcap[3], freq)
            amb_f, amb_r = ambient[freq]

            if drive == "front":
                near, far, amb_near, amb_far, found = f_amp, r_amp, amb_f, amb_r, f_at
            else:
                near, far, amb_near, amb_far, found = r_amp, f_amp, amb_r, amb_f, r_at

            point = SweepPoint(
                freq_hz=freq, drive=drive,
                near_g=near, far_g=far,
                ambient_near_g=amb_near, ambient_far_g=amb_far,
                found_hz=found,
            )
            if not fcap[0] or not rcap[0]:
                point.note = "a pod returned no samples"
            elif near < _MIN_SNR * max(amb_near, 1e-5):
                point.note = "driven pod at the noise floor — shaker output too low here"
            else:
                point.ok = True
                point.ratio_db = _db(far / near) if near > 0 else 0.0
                if far < _FLOOR_SNR * max(amb_far, 1e-5):
                    point.floor_limited = True
                    point.note = "far pod at the noise floor — true isolation is at least this good"
            result.points.append(point)
            tick()
            await asyncio.sleep(gap_s)

    curve = isolation_curve(result.points)
    result.summary = summarise(result.points, curve)
    result.summary["curve"] = curve
    result.ok = any(p.ok for p in result.points)
    if not result.ok:
        result.reason = "no point in the sweep rose above the noise floor"
    quiet = [p.freq_hz for p in result.points if not p.ok]
    if quiet:
        result.warnings.append(
            f"{len(quiet)} of {len(result.points)} points were too quiet to use "
            f"({min(quiet):.0f}-{max(quiet):.0f} Hz)"
        )
    return result


@dataclass
class SweepTask:
    """A sweep running in the background, with progress the UI can poll.

    Background rather than a long request because the measurement takes about a
    minute and the person being measured is sitting still for all of it. A
    progress bar is the difference between waiting and wondering whether it
    crashed.
    """

    running: bool = False
    done: int = 0
    total: int = 0
    started_at: float = 0.0
    result: SweepResult | None = None
    error: str | None = None
    _task: asyncio.Task[None] | None = field(default=None, repr=False, init=False)

    def start(self, bus: Any, hub: Any, **kwargs: Any) -> bool:
        """Begin a sweep. Returns False if one is already running."""
        if self.running:
            return False
        self.running = True
        self.done = 0
        self.total = 0
        self.result = None
        self.error = None
        self.started_at = time.monotonic()

        def progress(done: int, total: int) -> None:
            self.done, self.total = done, total

        async def go() -> None:
            try:
                self.result = await run(bus, hub, progress=progress, **kwargs)
            except Exception as exc:  # noqa: BLE001 - a sweep must never take the app down
                log.exception("sweep failed")
                self.error = str(exc)
            finally:
                self.running = False

        self._task = asyncio.create_task(go())
        return True

    def status(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started_at if self.started_at else 0.0
        remaining = None
        if self.running and self.done > 0 and self.total > 0:
            remaining = round(elapsed * (self.total - self.done) / self.done, 1)
        return {
            "running": self.running,
            "done": self.done,
            "total": self.total,
            "elapsed_s": round(elapsed, 1),
            "remaining_s": remaining,
            "error": self.error,
            "result": self.result.as_dict() if self.result else None,
        }
