#!/usr/bin/env python3
"""Sample /api/status and report the extremes of per-axle slip.

Nobody can read a phone at threshold braking. This polls the rig, tracks the
peaks, and prints a summary you can read afterwards — so the corner-ordering
check becomes "drive, then look" instead of "drive while looking".

Usage:
    python scripts/axle_probe.py                 # until Ctrl-C
    python scripts/axle_probe.py --seconds 60
    python scripts/axle_probe.py --host simrig-pi.local
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

# Slip below this is normal tyre behaviour, not an event worth reporting.
_NOISE_FLOOR_MPS = 0.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="simrig-pi.local")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl-C")
    ap.add_argument("--hz", type=float, default=20.0)
    args = ap.parse_args()

    url = f"http://{args.host}/api/status"
    interval = 1.0 / max(args.hz, 1.0)
    started = time.monotonic()

    # Extremes, plus the running state at zero pedal input for a baseline.
    peak = {"front_lock": 0.0, "front_spin": 0.0, "rear_lock": 0.0, "rear_spin": 0.0}
    cruise: list[tuple[float, float]] = []
    samples = 0
    no_axle_warned = False

    print(f"polling {url} at {args.hz:g} Hz — Ctrl-C to stop\n")
    print(f"{'speed':>7} {'gear':>4} {'front':>9} {'rear':>9}  state")
    try:
        while True:
            if args.seconds and time.monotonic() - started > args.seconds:
                break
            try:
                with urllib.request.urlopen(url, timeout=2.0) as r:
                    data = json.load(r)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"  (no reading: {exc})")
                time.sleep(1.0)
                continue

            axle = data.get("axle")
            if axle is None:
                if not no_axle_warned:
                    print("  /api/status has no 'axle' block — the Pi is running an "
                          "older build. Deploy main first.")
                    no_axle_warned = True
                time.sleep(1.0)
                continue

            front = float(axle.get("slip_front") or 0.0)
            rear = float(axle.get("slip_rear") or 0.0)
            tel = data.get("telemetry") or {}
            speed = float(tel.get("speed_kph") or 0.0)
            gear = tel.get("current_gear", "-")
            brake = int(tel.get("brake") or 0)
            throttle = int(tel.get("throttle") or 0)
            samples += 1

            peak["front_lock"] = min(peak["front_lock"], front)
            peak["front_spin"] = max(peak["front_spin"], front)
            peak["rear_lock"] = min(peak["rear_lock"], rear)
            peak["rear_spin"] = max(peak["rear_spin"], rear)
            if brake < 5 and throttle < 5 and speed > 30:
                cruise.append((front, rear))

            state = ""
            if brake > 40:
                state = f"BRAKING {brake * 100 // 255}%"
            elif throttle > 40:
                state = f"THROTTLE {throttle * 100 // 255}%"
            elif speed > 30:
                state = "coasting"
            if abs(front) > _NOISE_FLOOR_MPS or abs(rear) > _NOISE_FLOOR_MPS:
                print(f"{speed:7.1f} {gear:>4} {front:+9.2f} {rear:+9.2f}  {state}")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass

    print(f"\n--- {samples} samples ---")
    if not samples:
        return 1
    print(f"  braking (most negative):   front {peak['front_lock']:+.2f}   "
          f"rear {peak['rear_lock']:+.2f}  m/s")
    print(f"  power-on (most positive):  front {peak['front_spin']:+.2f}   "
          f"rear {peak['rear_spin']:+.2f}  m/s")
    if cruise:
        n = len(cruise)
        print(f"  coasting baseline ({n} samples):  "
              f"front {sum(f for f, _ in cruise) / n:+.2f}   "
              f"rear {sum(r for _, r in cruise) / n:+.2f}  m/s   (both should be near 0)")
    else:
        print("  coasting baseline: no samples — coast at steady speed off both pedals")

    print("\n--- reading ---")
    fl, rl = peak["front_lock"], peak["rear_lock"]
    fs, rs = peak["front_spin"], peak["rear_spin"]
    if abs(fl) > abs(rl) * 1.3:
        print("  braking: front slips more than rear — consistent with correct ordering")
    elif abs(rl) > abs(fl) * 1.3:
        print("  braking: REAR slips more than front — suspect swapped axle pairs")
    else:
        print("  braking: front and rear similar — inconclusive, the power-on test decides")
    if rs > max(fs, _NOISE_FLOOR_MPS) * 1.3:
        print("  power-on: rear spins, front does not — correct for a rear-wheel-drive car")
    elif fs > max(rs, _NOISE_FLOOR_MPS) * 1.3:
        print("  power-on: FRONT spins in a rear-drive car — axle pairs are swapped")
    else:
        print("  power-on: no clear wheelspin captured — try again with more throttle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
