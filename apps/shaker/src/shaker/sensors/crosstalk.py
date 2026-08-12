"""Measure how much of each shaker's output reaches the other pod.

Two channels only buy anything if your body can tell them apart, and below
about 100 Hz it cannot localise the source — it reports which part of you is
loaded. Feet or back. So mechanical coupling through the frame does not merely
blur the stereo image, it converts a rear reading into a front one: energy from
the pedal-deck shaker that reaches the seat is felt in the back, which is
exactly the cue that was supposed to mean "rear".

That makes the front/rear isolation a number worth having rather than a matter
of opinion. Some coupling is realistic — a real chassis carries a kerb strike
from the front axle to the seat — so the question is never whether it exists
but what the ratio is.

The stimulus is the existing wiring check: one channel at a time, bypassing
master gain, trim and the limiter, so both pulses are identical in software and
any difference measured is purely mechanical. Levels are integrated over a
window inside each pulse rather than read off the live meter, which is smoothed
for a 2 Hz UI and would smear the two pulses into each other.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Pulse and gap handed to the wiring check. Longer than the interactive default
# so there is room to skip the onset and still integrate for about a second.
PULSE_S = 1.5
GAP_S = 0.8

# Skipped at the start of each pulse: one audio block of trigger latency, the
# output buffer, and whatever the amplifier and shaker take to reach steady
# state. Generous because the cost is only measurement time.
_SETTLE_S = 0.4

# Integrated per pulse, ending before the pulse does so a late start cannot run
# the window off the end into silence.
_WINDOW_S = 0.95

# Ambient measured before the stimulus, and subtracted in power below.
_BASELINE_S = 0.6

# The driven pod must exceed ambient by this much for the ratio to mean
# anything. Below it the measurement is of the room, not the rig.
_MIN_SNR = 3.0

# Isolation bands. Chosen from what the two channels are for rather than from
# any standard: past -12 dB the far pod contributes a hint of chassis
# continuity, and by -6 dB it is carrying enough to blur which body region is
# being addressed.
_GOOD_DB = -12.0
_USABLE_DB = -6.0


def ratio_db(far_g: float, near_g: float) -> float:
    """Crosstalk in dB. Floored rather than allowed to reach -inf."""
    if near_g <= 0.0:
        return 0.0
    return 20.0 * math.log10(max(far_g, 1e-6) / near_g)


def subtract_baseline(measured_g: float, baseline_g: float) -> float:
    """Remove ambient in power, since noise and signal add as energy not amplitude."""
    return math.sqrt(max(measured_g * measured_g - baseline_g * baseline_g, 0.0))


def classify(worst_db: float) -> tuple[str, str]:
    if worst_db <= _GOOD_DB:
        return ("good", "Well separated. The coupling that remains reads as chassis "
                        "continuity rather than blurring the two channels.")
    if worst_db <= _USABLE_DB:
        return ("usable", "Some bleed, but each shaker still dominates its own end. "
                          "Worth a cheap improvement, not surgery.")
    return ("poor", "The two channels are arriving at both ends at similar level, so "
                    "the rig is closer to mono than stereo. Separating the effects by "
                    "frequency costs nothing and recovers most of the distinction.")


@dataclass
class CrosstalkResult:
    ok: bool = False
    reason: str | None = None
    baseline_front_g: float = 0.0
    baseline_rear_g: float = 0.0
    # Levels at each pod while the named channel is the only one driven.
    front_drive_front_g: float = 0.0
    front_drive_rear_g: float = 0.0
    rear_drive_rear_g: float = 0.0
    rear_drive_front_g: float = 0.0
    front_to_rear_db: float = 0.0
    rear_to_front_db: float = 0.0
    verdict: str = ""
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "baseline": {
                "front_g": round(self.baseline_front_g, 5),
                "rear_g": round(self.baseline_rear_g, 5),
            },
            "front_drive": {
                "front_g": round(self.front_drive_front_g, 5),
                "rear_g": round(self.front_drive_rear_g, 5),
            },
            "rear_drive": {
                "front_g": round(self.rear_drive_front_g, 5),
                "rear_g": round(self.rear_drive_rear_g, 5),
            },
            "front_to_rear_db": round(self.front_to_rear_db, 1),
            "rear_to_front_db": round(self.rear_to_front_db, 1),
            "verdict": self.verdict,
            "detail": self.detail,
            "warnings": self.warnings,
        }


async def measure(
    bus: Any,
    hub: Any,
    pulse_s: float = PULSE_S,
    gap_s: float = GAP_S,
    settle_s: float = _SETTLE_S,
    window_s: float = _WINDOW_S,
    baseline_s: float = _BASELINE_S,
) -> CrosstalkResult:
    """Run the two-pulse sequence and report both directions.

    Timings are parameters so tests can run the whole sequence in a fraction of
    a second against a simulated bus.
    """
    result = CrosstalkResult()

    pods = {p.name: p for p in hub._pods}
    front, rear = pods.get("front"), pods.get("rear")
    if front is None or rear is None:
        result.reason = "both pods are required"
        return result
    if not (front.stats.present and rear.stats.present):
        missing = [n for n, p in (("front", front), ("rear", rear)) if not p.stats.present]
        result.reason = f"not detected: {', '.join(missing)}"
        return result

    async def window(seconds: float) -> tuple[float, float]:
        front.begin_window()
        rear.begin_window()
        await asyncio.sleep(seconds)
        f, fn = front.end_window()
        r, rn = rear.end_window()
        if fn == 0 or rn == 0:
            result.warnings.append("a pod returned no samples during a window")
        return (f, r)

    # Ambient first. A rig being leaned on, or a fan, sets a floor that would
    # otherwise be read as coupling.
    base_f, base_r = await window(baseline_s)
    result.baseline_front_g, result.baseline_rear_g = base_f, base_r

    bus.trigger_wiring_check(pulse_s=pulse_s, gap_s=gap_s)

    await asyncio.sleep(settle_s)
    f_drive_f, f_drive_r = await window(window_s)

    # Remaining front pulse, then the gap, then the rear pulse's settle.
    await asyncio.sleep(max(0.0, pulse_s - settle_s - window_s) + gap_s + settle_s)
    r_drive_f, r_drive_r = await window(window_s)

    result.front_drive_front_g = subtract_baseline(f_drive_f, base_f)
    result.front_drive_rear_g = subtract_baseline(f_drive_r, base_r)
    result.rear_drive_front_g = subtract_baseline(r_drive_f, base_f)
    result.rear_drive_rear_g = subtract_baseline(r_drive_r, base_r)

    # Guard before reporting a ratio. A near-silent driven pod means the shaker,
    # amplifier or mount is the problem, and a crosstalk figure computed from it
    # would be noise over noise dressed up as a measurement.
    quiet = []
    if result.front_drive_front_g < _MIN_SNR * max(base_f, 1e-4):
        quiet.append("front")
    if result.rear_drive_rear_g < _MIN_SNR * max(base_r, 1e-4):
        quiet.append("rear")
    if quiet:
        result.reason = (
            f"{' and '.join(quiet)} barely moved during its own pulse — check the amplifier "
            "channel, the speaker leads and the mount before reading anything into a ratio"
        )
        return result

    result.front_to_rear_db = ratio_db(result.front_drive_rear_g, result.front_drive_front_g)
    result.rear_to_front_db = ratio_db(result.rear_drive_front_g, result.rear_drive_rear_g)

    worst = max(result.front_to_rear_db, result.rear_to_front_db)
    result.verdict, result.detail = classify(worst)
    result.ok = True

    if abs(result.front_to_rear_db - result.rear_to_front_db) > 6.0:
        result.warnings.append(
            "the two directions differ markedly, which is normal — a seat couples into a "
            "body far better than a pedal deck does"
        )
    log.info(
        "crosstalk: front->rear %.1f dB, rear->front %.1f dB (%s)",
        result.front_to_rear_db, result.rear_to_front_db, result.verdict,
    )
    return result
