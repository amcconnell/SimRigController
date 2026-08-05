#!/usr/bin/env python3
"""Regenerate src/shaker/gt7/drivetrain.py from the racetrace car database.

The shaker needs one fact per car — where the engine sits and which axle is
driven — to place gear-shift and engine effects on the right shaker. GT7's
telemetry carries a car code but nothing about the car, so the mapping has to
come from outside.

The source of truth lives in the sibling racetrace project, which scraped it
from dg-edge.com and keys it by the same GT7 car code this app already
receives. We vendor a derived table rather than importing racetrace at
runtime: the ansible deploy only ships apps/shaker to the Pi, so a runtime
dependency on a sibling repo would simply be absent in production. The
extract is ~8 KB, so vendoring costs nothing and keeps the Pi self-contained.

Usage:
    python scripts/gen_drivetrain.py [path/to/racetrace]
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_RACETRACE = Path.home() / "Documents" / "Projects" / "racetrace"
_OUT = Path(__file__).resolve().parent.parent / "src" / "shaker" / "gt7" / "drivetrain.py"

# dg-edge's prose descriptions -> the compact codes racetrace uses. Anything
# outside this map (the database has one "-" placeholder) is dropped rather
# than guessed at.
_LAYOUTS = {
    "front engine, rear wheel drive": "FR",
    "mid engine, rear wheel drive": "MR",
    "rear engine, rear wheel drive": "RR",
    "front engine, front wheel drive": "FF",
    "four wheel drive": "4WD",
}

_HEADER = '''"""Per-car drivetrain layout, keyed by GT7 car code.

GENERATED FILE — do not edit by hand. Regenerate with:
    python scripts/gen_drivetrain.py

Source: {source}, scraped {scraped_at}, via the racetrace project's
data/car_specs.json. {count} cars.

The car code arrives in the telemetry packet at offset 0x124. Coverage is not
complete — newer DLC and rare cars are missing, and every lookup here can
return None. Callers must treat an unknown car as "no opinion" and fall back
to their configured defaults, never as a guess.
"""

from __future__ import annotations

# GT7 car code -> layout. FR/MR/RR = front/mid/rear engine, rear wheel drive;
# FF = front engine, front wheel drive; 4WD = four wheel drive.
_LAYOUT: dict[int, str] = {{
{table}
}}

# Which axle takes the driveline shock of a gear change. This, not the layout
# label, is what decides where a shift thump belongs.
_DRIVEN_AXLE = {{
    "FF": "front",
    "FR": "rear",
    "MR": "rear",
    "RR": "rear",
    "4WD": "both",
}}

# Where the engine physically sits, for placing engine rumble. Deliberately
# absent for 4WD: the source database records drive type but not engine
# position, and 4WD spans both extremes (a GT-R and a Veyron are both "Four
# Wheel Drive"). Returning None there is honest; guessing would put a
# mid-engine car's thrum under the pedals.
_ENGINE_POSITION = {{
    "FF": "front",
    "FR": "front",
    "MR": "rear",
    "RR": "rear",
}}


def layout_for(car_code: int | None) -> str | None:
    """Layout code for a GT7 car, or None if unknown."""
    if car_code is None:
        return None
    return _LAYOUT.get(car_code)


def driven_axle(car_code: int | None) -> str | None:
    """"front", "rear", "both", or None when the car isn't in the table."""
    layout = layout_for(car_code)
    return _DRIVEN_AXLE.get(layout) if layout else None


def engine_position(car_code: int | None) -> str | None:
    """"front", "rear", or None — None also for 4WD, where it isn't recorded."""
    layout = layout_for(car_code)
    return _ENGINE_POSITION.get(layout) if layout else None
'''


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_RACETRACE
    specs_path = root / "data" / "car_specs.json"
    if not specs_path.is_file():
        print(f"car specs not found at {specs_path}", file=sys.stderr)
        print("pass the racetrace checkout as the first argument", file=sys.stderr)
        return 1

    raw = json.loads(specs_path.read_text())
    cars = raw["cars"]

    table: dict[int, str] = {}
    skipped: list[str] = []
    for code, spec in cars.items():
        layout = _LAYOUTS.get(str(spec.get("drivetrain_layout", "")).strip().lower())
        if layout is None:
            skipped.append(f"{code} {spec.get('name', '?')}")
            continue
        table[int(code)] = layout

    # Two per line, sorted by code — keeps the diff readable when the source
    # database is rescraped and a handful of cars change.
    items = sorted(table.items())
    lines = [
        "    " + "  ".join(f"{code}: {layout!r}," for code, layout in items[i:i + 4])
        for i in range(0, len(items), 4)
    ]

    _OUT.write_text(_HEADER.format(
        source=raw.get("source", "unknown"),
        scraped_at=raw.get("scraped_at", "unknown"),
        count=len(table),
        table="\n".join(lines),
    ))

    counts: dict[str, int] = {}
    for layout in table.values():
        counts[layout] = counts.get(layout, 0) + 1
    print(f"wrote {_OUT.relative_to(_OUT.parents[4])}: {len(table)} cars")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if skipped:
        print(f"  skipped {len(skipped)} with no usable layout: {', '.join(skipped[:5])}")
    generated = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"  generated {generated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
