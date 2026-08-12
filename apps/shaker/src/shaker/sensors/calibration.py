"""Correct a pod's sensitivity against the one reference every rig has.

Gravity is 1 g, everywhere, for free. A still pod reporting 0.908 g is not
seeing a weaker planet — its sensitivity is off, and the ADXL345 is specified
loosely enough for that to be entirely legal: 256 LSB/g typical against a
230-282 spread, so two parts from the same bag can disagree by ten percent.

Most of what the pods are for survives that. Orientation, tap testing, and the
shape of one pod's response across frequency are all unaffected by a constant
factor. Crosstalk is not: it divides one pod's reading by the other's, so two
independent sensitivity errors land directly on a number whose interesting
bands are six decibels wide.

What this cannot do is separate sensitivity error from per-axis offset — one
orientation gives one equation. Properly untangling them wants a six-position
tumble, which is a lot of ceremony for a rig where the pods are about to be
bolted down and never moved. A single factor makes the two pods agree with each
other and with gravity, which is what the measurements actually depend on.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Sampled while the pod sits still. Long enough to average out a desk, short
# enough that nobody has to stand there.
_WINDOW_S = 2.0
_INTERVAL_S = 0.1

# The pod must not be moving. Two nets, because they catch different things.
#
# Drift in the gravity magnitude catches a pod slowly re-orientated — but only
# slowly, since the gravity tracker is a 1.5 s average and lags a hand.
_MAX_DRIFT_G = 0.02

# Movement therefore shows up mostly as AC, because a sample that has moved
# differs from a gravity estimate that has not yet followed it. So this is the
# net that actually catches being picked up, and it is set well above a bench
# floor (measured at 0.04 g on this rig) and well below a hand (0.24 g).
_MAX_VIBRATION_G = 0.10

# Outside this, the reading is a fault rather than a part to trim. The ADXL345
# spread tops out near +/-10%; anything past 25% means a wrong range, a wrong
# scale constant, or a pod that is not sitting still after all, and quietly
# dividing it out would hide the real problem.
_MIN_MEASURED_G = 0.75
_MAX_MEASURED_G = 1.25


@dataclass
class CalibrationResult:
    ok: bool = False
    pod: str = ""
    reason: str | None = None
    measured_g: float = 0.0
    drift_g: float = 0.0
    vibration_g: float = 0.0
    previous_scale: float = 1.0
    scale: float = 1.0
    error_pct: float = 0.0
    samples: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "pod": self.pod,
            "reason": self.reason,
            "measured_g": round(self.measured_g, 4),
            "drift_g": round(self.drift_g, 4),
            "vibration_g": round(self.vibration_g, 4),
            "previous_scale": round(self.previous_scale, 5),
            "scale": round(self.scale, 5),
            "error_pct": round(self.error_pct, 2),
            "samples": self.samples,
        }


@dataclass
class CalibrationRun:
    results: list[CalibrationResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.results) and all(r.ok for r in self.results),
            "pods": [r.as_dict() for r in self.results],
        }


async def calibrate(
    hub: Any,
    pod_name: str | None = None,
    window_s: float = _WINDOW_S,
    interval_s: float = _INTERVAL_S,
) -> CalibrationRun:
    """Measure each present pod against gravity and set its scale.

    Calibrates every present pod when `pod_name` is None, so one press covers a
    rig whose second pod was wired later. Absent pods are skipped rather than
    reported as failures — there is nothing wrong with a one-pod rig.
    """
    run = CalibrationRun()
    pods = [p for p in hub._pods if pod_name is None or p.name == pod_name]
    if pod_name is not None and not pods:
        run.results.append(CalibrationResult(pod=pod_name, reason="no such pod"))
        return run

    present = [p for p in pods if p.stats.present]
    if not present:
        run.results.append(
            CalibrationResult(pod=pod_name or "any", reason="no pod is detected")
        )
        return run

    # Sample them together: one wait covers every pod, and a rig disturbed
    # mid-window disturbs all of them equally, which the drift check then sees.
    tilts: dict[str, list[float]] = {p.name: [] for p in present}
    vibes: dict[str, list[float]] = {p.name: [] for p in present}
    steps = max(2, int(window_s / interval_s))
    for _ in range(steps):
        for p in present:
            tilts[p.name].append(p.stats.tilt_g)
            vibes[p.name].append(p.stats.vibration_rms_g)
        await asyncio.sleep(interval_s)

    for p in present:
        run.results.append(_evaluate(hub, p, tilts[p.name], vibes[p.name]))
    return run


def _evaluate(hub: Any, pod: Any, tilts: list[float], vibes: list[float]) -> CalibrationResult:
    r = CalibrationResult(pod=pod.name, previous_scale=pod.scale, scale=pod.scale)
    r.samples = len(tilts)
    if not tilts:
        r.reason = "no samples"
        return r

    measured = sum(tilts) / len(tilts)
    r.measured_g = measured
    r.drift_g = max(tilts) - min(tilts)
    r.vibration_g = max(vibes) if vibes else 0.0

    if r.drift_g > _MAX_DRIFT_G:
        r.reason = (
            f"{pod.name} moved during the measurement (gravity wandered by "
            f"{r.drift_g:.3f} g) — set it down and leave it alone"
        )
        return r
    if r.vibration_g > _MAX_VIBRATION_G:
        r.reason = f"{pod.name} is being shaken ({r.vibration_g:.2f} g) — calibrate it at rest"
        return r
    if not (_MIN_MEASURED_G <= measured <= _MAX_MEASURED_G):
        r.reason = (
            f"{pod.name} reads {measured:.3f} g at rest, too far from 1 g to be part "
            "tolerance — check the range and mounting before trimming it away"
        )
        return r

    # The scale multiplies the pod's existing correction rather than replacing
    # it, so re-calibrating an already-calibrated pod converges instead of
    # oscillating between two half-corrections.
    r.scale = pod.scale / measured
    r.error_pct = (measured - 1.0) * 100.0
    hub.set_scale(pod.name, r.scale)
    r.ok = True
    log.info(
        "calibrated %s: read %.4f g, scale %.5f -> %.5f (%.2f%% off)",
        pod.name, measured, r.previous_scale, r.scale, r.error_pct,
    )
    return r
