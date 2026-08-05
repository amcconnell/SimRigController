#!/usr/bin/env python3
"""Record body-motion telemetry beside derived references, then identify it.

GT7 sends sway/heave/surge and documents none of them. Acceleration, velocity
and displacement are all plausible and the reference frame is unknown, so this
samples them alongside quantities whose meaning IS established — d(speed)/dt
and v * yaw_rate — and afterwards reports which hypothesis each field fits.

    python scripts/motion_probe.py record --seconds 300
    python scripts/motion_probe.py analyse

Drive deliberately: hard straight-line braking, then a long steady corner.
Separated manoeuvres discriminate far better than a fast lap.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import time
import urllib.error
import urllib.request

LOG = pathlib.Path(__file__).resolve().parent / "motion_log.jsonl"


def record(host: str, seconds: float, hz: float) -> int:
    url = f"http://{host}/api/status"
    interval, started, n = 1.0 / hz, time.monotonic(), 0
    with LOG.open("w") as fh:
        print(f"recording {url} at {hz:g} Hz -> {LOG.name}   Ctrl-C to stop")
        try:
            while seconds <= 0 or time.monotonic() - started < seconds:
                try:
                    with urllib.request.urlopen(url, timeout=2.0) as r:
                        d = json.load(r)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    time.sleep(0.5)
                    continue
                m, t = d.get("motion"), d.get("telemetry") or {}
                if not m or not m.get("has_motion"):
                    time.sleep(0.5)
                    continue
                fh.write(json.dumps({
                    "t": round(time.monotonic() - started, 3),
                    "surge": m["surge"], "sway": m["sway"], "heave": m["heave"],
                    "long_accel": m["long_accel"], "lat_accel": m["lat_accel"],
                    "speed": t.get("speed_kph", 0.0) / 3.6,
                    "throttle": t.get("throttle", 0), "brake": t.get("brake", 0),
                }) + "\n")
                n += 1
                if n % 100 == 0:
                    print(f"  {n} samples, {time.monotonic()-started:.0f}s")
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    print(f"\n{n} samples written to {LOG}")
    return 0


def _corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 10:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def _fit(a: list[float], b: list[float]) -> float:
    """Least-squares scale of b onto a (a ~= k*b), through the origin."""
    den = sum(y * y for y in b)
    return sum(x * y for x, y in zip(a, b)) / den if den > 0 else 0.0


def analyse() -> int:
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    if len(rows) < 50:
        print(f"only {len(rows)} samples — drive some more")
        return 1
    print(f"{len(rows)} samples over {rows[-1]['t']:.0f}s\n")

    speed = [r["speed"] for r in rows]
    # Integral of speed: the displacement hypothesis, mean-removed so a
    # monotonic ramp does not fake a correlation with anything rising.
    dist, acc = [], 0.0
    for r in rows:
        acc += r["speed"] * 0.05
        dist.append(acc)
    md = sum(dist) / len(dist)
    dist = [d - md for d in dist]

    for field, ref, ref_name in (("surge", "long_accel", "d(speed)/dt"),
                                 ("sway", "lat_accel", "v x yaw rate")):
        f = [r[field] for r in rows]
        ref_v = [r[ref] for r in rows]
        print(f"=== {field} ===")
        for hyp, series in (("acceleration", ref_v), ("velocity", speed), ("displacement", dist)):
            c = _corr(f, series)
            print(f"  vs {hyp:14} r = {c:+.3f}")
        k = _fit(f, ref_v)
        print(f"  best fit: {field} = {k:+.3f} x {ref_name}")
        if abs(_corr(f, ref_v)) > 0.8:
            sign = "SAME sign" if k > 0 else "OPPOSITE sign (negated)"
            print(f"  -> tracks {ref_name}, {sign}, scale {abs(k):.2f}")
        else:
            print("  -> does not track the acceleration reference")
        print()

    braking = [r for r in rows if r["brake"] > 60]
    if braking:
        s = sum(r["surge"] for r in braking) / len(braking)
        a = sum(r["long_accel"] for r in braking) / len(braking)
        print(f"under braking ({len(braking)} samples): surge {s:+.2f}, d(speed)/dt {a:+.2f}")
    coast = [r for r in rows if r["brake"] < 5 and r["throttle"] < 5 and r["speed"] > 15]
    if coast:
        print(f"coasting ({len(coast)} samples): surge "
              f"{sum(r['surge'] for r in coast)/len(coast):+.2f}, "
              f"heave {sum(r['heave'] for r in coast)/len(coast):+.2f}  (both should be near 0)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["record", "analyse"])
    ap.add_argument("--host", default="simrig-pi.local")
    ap.add_argument("--seconds", type=float, default=0.0)
    ap.add_argument("--hz", type=float, default=20.0)
    a = ap.parse_args()
    return record(a.host, a.seconds, a.hz) if a.mode == "record" else analyse()


if __name__ == "__main__":
    raise SystemExit(main())
