"""What sway/heave/surge actually are, pinned against a real capture.

GT7 documents none of the three. They were identified on a live console on
2026-08-05 by recording them beside two references whose meaning is
established — d(speed)/dt and v * yaw_rate — and the capture is committed at
tests/data/motion_log.jsonl: 665 samples over 98 seconds, ending in a rollover
that peaked at 2.9 g vertical.

These tests are the finding with teeth. The conclusion is written up in
protocol.py, but prose in a comment is exactly what let the inverted
wheel-rotation sign survive for months — a claim nobody could run. Anything
that contradicts what the fields mean will fail here instead.

The fixture is recorded app output, so it does not exercise the parser. What
it pins is the interpretation: the relationships that any motion cueing built
on these fields will assume.
"""

from __future__ import annotations

import json
import math
import pathlib
import statistics

import pytest

_LOG = pathlib.Path(__file__).parent / "data" / "motion_log.jsonl"
_CRASH_T = 92.0        # the rollover; body axis rotates through gravity after this


@pytest.fixture(scope="module")
def samples() -> list[dict]:
    rows = [json.loads(line) for line in _LOG.read_text().splitlines() if line.strip()]
    assert len(rows) > 600, "fixture truncated"
    return rows


def _corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def _scale(a: list[float], b: list[float]) -> float:
    """Least-squares fit of a onto b through the origin."""
    return sum(x * y for x, y in zip(a, b)) / sum(y * y for y in b)


# --- surge -------------------------------------------------------------------


def test_surge_is_longitudinal_acceleration(samples: list[dict]) -> None:
    """Compared only on straight-line samples, because that is where the
    reference is valid: d(speed)/dt is path-tangential while surge is
    body-longitudinal, and the two diverge with sideslip. Fitting across
    corners gives 0.77 and looks like a scale factor when it is a frame
    difference — which is why the naive whole-capture fit is misleading.
    """
    straight = [r for r in samples if abs(r["lat_accel"]) < 1.5 and r["speed"] > 10]
    assert len(straight) > 100

    surge = [r["surge"] for r in straight]
    ref = [r["long_accel"] for r in straight]
    assert _corr(surge, ref) > 0.95
    assert _scale(surge, ref) == pytest.approx(1.0, abs=0.05)


def test_surge_sign_is_negative_under_braking(samples: list[dict]) -> None:
    """Negative means decelerating — same convention as the reference, so no
    negation is needed anywhere downstream."""
    braking = [r for r in samples if r["brake"] > 60 and r["t"] < _CRASH_T - 4]
    assert len(braking) > 40
    assert statistics.mean(r["surge"] for r in braking) < -5.0


# --- sway --------------------------------------------------------------------


def test_sway_is_lateral_acceleration_but_negated(samples: list[dict]) -> None:
    """The sign relationship is the load-bearing part: a cueing algorithm that
    assumed the same convention as v * yaw_rate would roll the platform the
    wrong way in every corner — and that reads as "feels odd", not as a bug."""
    hard = [r for r in samples if abs(r["lat_accel"]) > 4.0]
    assert len(hard) > 300

    opposed = sum(1 for r in hard if (r["sway"] > 0) != (r["lat_accel"] > 0))
    assert opposed / len(hard) > 0.95, "sway sign convention is not consistently opposed"
    assert _corr([r["sway"] for r in hard], [r["lat_accel"] for r in hard]) < -0.9


# --- heave -------------------------------------------------------------------


def test_heave_excludes_gravity(samples: list[dict]) -> None:
    """A field carrying gravity would sit near +/-9.81 at rest. This sits at
    zero, at both 36 and 90+ km/h, so it is dynamic vertical acceleration only
    and needs no 1 g offset removed before use."""
    calm = [
        r for r in samples
        if r["t"] < _CRASH_T - 4 and abs(r["surge"]) < 3 and abs(r["sway"]) < 3
    ]
    assert len(calm) > 50
    assert statistics.median(r["heave"] for r in calm) == pytest.approx(0.0, abs=0.3)

    pre = [r for r in samples if r["t"] < _CRASH_T - 4]
    near_g = [r for r in pre if -10.5 <= r["heave"] <= -9.0]
    assert len(near_g) / len(pre) < 0.02


# --- the rollover ------------------------------------------------------------


def test_capture_contains_the_rollover(samples: list[dict]) -> None:
    """The crash is why this fixture is worth keeping. It is the only part of
    the capture that exercises the tail of the range, and any cueing algorithm
    needs to survive it without producing a platform command that would hurt
    somebody. 2.9 g vertical is the number to design limits against.
    """
    peak_heave = max(abs(r["heave"]) for r in samples)
    peak_sway = max(abs(r["sway"]) for r in samples)
    assert peak_heave > 25.0
    assert peak_sway > 20.0

    tail = [r for r in samples if r["t"] >= _CRASH_T]
    # Body-vertical rotating through gravity as the car barrel-rolls: heave
    # sweeps across most of a g in both directions within a few seconds.
    assert min(r["heave"] for r in tail) < -9.0
    assert max(r["heave"] for r in tail) > 9.0


def test_fields_stay_finite_through_the_crash(samples: list[dict]) -> None:
    """No NaN or inf at 3 g — worth pinning, since a cueing filter fed a NaN
    would propagate it into an actuator command and stay there."""
    for r in samples:
        for k in ("surge", "sway", "heave", "long_accel", "lat_accel"):
            assert math.isfinite(r[k]), f"{k} not finite at t={r['t']}"
