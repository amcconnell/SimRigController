#!/usr/bin/env python3
"""Find and read the accelerometer pods from a terminal, without the app.

For the half hour when a pod is in one hand and a screwdriver in the other, and
the question is whether the wiring is right before anything is bolted down.

    python scripts/pod_probe.py                # what is on the bus
    python scripts/pod_probe.py --watch        # live, until Ctrl-C
    python scripts/pod_probe.py --tap          # peak hold, for whacking the frame

`i2cdetect -y 1` answers a narrower question — whether *something* acknowledges.
This checks the device ID, so a floating or shorted bus reads as absent rather
than as a sensor reporting a convincing zero.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shaker.sensors.adxl345 import (            # noqa: E402
    ADDR_ALT, ADDR_PRIMARY, ADXL345, SensorError, open_bus,
)

_NAMES = {ADDR_PRIMARY: "front (ALT low)", ADDR_ALT: "rear (ALT high)"}


def _orientation(x: float, y: float, z: float) -> str:
    name, value = max((("X", x), ("Y", y), ("Z", z)), key=lambda a: abs(a[1]))
    return f"{name}{'+' if value > 0 else '-'}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", type=int, default=1)
    ap.add_argument("--watch", action="store_true", help="stream readings until Ctrl-C")
    ap.add_argument("--tap", action="store_true", help="hold the peak, for tap-testing")
    ap.add_argument("--hz", type=float, default=5.0, help="refresh rate when watching")
    args = ap.parse_args(argv)

    try:
        bus = open_bus(args.bus)
    except SensorError as exc:
        print(f"cannot open the bus: {exc}", file=sys.stderr)
        print("\nOn a Pi: check `dtparam=i2c_arm=on` in /boot/firmware/config.txt,"
              "\nthat i2c-dev is loaded, and that you are in the i2c group.", file=sys.stderr)
        return 2

    found = []
    for addr in (ADDR_PRIMARY, ADDR_ALT):
        dev = ADXL345(bus, addr)
        label = _NAMES[addr]
        if dev.probe():
            dev.configure()
            found.append(dev)
            print(f"0x{addr:02x}  {label:<18} FOUND  ({dev.rate_hz:.0f} Hz, +/-{dev.range_g} g)")
        else:
            print(f"0x{addr:02x}  {label:<18} -")

    if not found:
        print("\nNothing answered. Check 3V3 and GND first, then SDA/SCL, then that CS is"
              "\ntied high — the part comes up in SPI mode otherwise and ignores I2C.")
        return 1

    if not (args.watch or args.tap):
        print()
        for dev in found:
            s = dev.read_one()
            mag = math.sqrt(s.x**2 + s.y**2 + s.z**2)
            print(f"0x{dev.address:02x}  x {s.x:+.3f}  y {s.y:+.3f}  z {s.z:+.3f}   "
                  f"|g| {mag:.3f}  facing {_orientation(s.x, s.y, s.z)}")
        return 0

    peaks = {d.address: 0.0 for d in found}
    print("\nCtrl-C to stop\n")
    try:
        while True:
            parts = []
            for dev in found:
                samples = dev.read_fifo()
                if not samples:
                    continue
                mean = [sum(getattr(s, ax) for s in samples) / len(samples)
                        for ax in ("x", "y", "z")]
                # Gravity is whatever the batch averages to; the interesting
                # number is what is left once it is removed.
                ac = max(
                    math.sqrt(sum((getattr(s, ax) - mean[i]) ** 2 for i, ax in enumerate("xyz")))
                    for s in samples
                )
                peaks[dev.address] = max(peaks[dev.address] * (0.0 if not args.tap else 1.0), ac)
                mag = math.sqrt(sum(v * v for v in mean))
                parts.append(
                    f"0x{dev.address:02x} |g| {mag:5.3f} {_orientation(*mean):>2}  "
                    f"ac {ac:6.3f}" + (f"  peak {peaks[dev.address]:6.3f}" if args.tap else "")
                )
            print("   ".join(parts) + "        ", end="\r", flush=True)
            time.sleep(1.0 / max(args.hz, 0.5))
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
