"""Host health: temperature, CPU and memory.

Worth surfacing on a box doing real-time audio, because the failure it warns
about is not a crash. A Pi that gets hot throttles its clock, and a throttled
Pi misses audio callbacks — which arrives as intermittent glitching in the
seat, indistinguishable from a DSP bug and far harder to find. Seeing the
temperature climb turns that from a mystery into a reading.

Everything is read from /proc and /sys, so it costs a few file reads and no
dependencies. Everything is also optional: none of these paths exist on the
Mac this is developed on, and a missing file is reported as None rather than
allowed to fail a status request that also carries the telemetry.

There was a fan reading here too, published by a script that drove the Argon
case's controller. The case is gone — it was the heatsink as much as the fan,
and losing it moved this Pi from the low 40s to the low 50s, still far below
the 60 C where throttling starts to matter.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
_STAT_PATH = Path("/proc/stat")
_MEMINFO_PATH = Path("/proc/meminfo")
_LOADAVG_PATH = Path("/proc/loadavg")

# A Pi 4 begins reducing clock around 60 C and throttles hard near 80. The
# first is where an audio callback starts being at risk, so that is where the
# UI should start caring rather than at the hard limit.
WARN_TEMP_C = 60.0
HOT_TEMP_C = 75.0


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, ValueError):
        return None


class SystemStats:
    """Host readings, with the one piece of state a CPU percentage needs.

    CPU utilisation is a rate, so it can only be derived from two samples.
    The previous totals are kept here and the figure covers the gap since the
    last call — which, given the UI polls twice a second, is the half second
    someone is actually looking at rather than an average since boot.
    """

    def __init__(self) -> None:
        self._prev_busy: float | None = None
        self._prev_total: float | None = None

    def read(self) -> dict[str, Any]:
        return {
            "cpu_temp_c": self.cpu_temp_c(),
            "cpu_pct": self.cpu_pct(),
            "load_1m": self.load_1m(),
            **self.memory(),
            "warn_temp_c": WARN_TEMP_C,
            "hot_temp_c": HOT_TEMP_C,
        }

    def cpu_temp_c(self) -> float | None:
        raw = _read(_TEMP_PATH)
        if raw is None:
            return None
        try:
            return round(int(raw.strip()) / 1000.0, 1)
        except ValueError:
            return None

    def cpu_pct(self) -> float | None:
        raw = _read(_STAT_PATH)
        if raw is None:
            return None
        for line in raw.splitlines():
            if not line.startswith("cpu "):
                continue
            try:
                parts = [float(x) for x in line.split()[1:]]
            except ValueError:
                return None
            if len(parts) < 5:
                return None
            total = sum(parts)
            # idle + iowait: the processor was available, whatever it was
            # waiting for. Counting iowait as busy would make a slow SD card
            # look like a CPU problem.
            busy = total - (parts[3] + parts[4])
            prev_busy, prev_total = self._prev_busy, self._prev_total
            self._prev_busy, self._prev_total = busy, total
            if prev_total is None or total <= prev_total:
                return None       # first call, or the counters were reset
            return round(100.0 * (busy - prev_busy) / (total - prev_total), 1)
        return None

    def load_1m(self) -> float | None:
        raw = _read(_LOADAVG_PATH)
        if raw is None:
            return None
        try:
            return float(raw.split()[0])
        except (ValueError, IndexError):
            return None

    def memory(self) -> dict[str, Any]:
        raw = _read(_MEMINFO_PATH)
        if raw is None:
            return {"mem_used_mb": None, "mem_total_mb": None, "mem_pct": None}
        fields: dict[str, int] = {}
        for line in raw.splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                try:
                    fields[key] = int(rest.split()[0])
                except (ValueError, IndexError):
                    pass
        total_kb = fields.get("MemTotal")
        avail_kb = fields.get("MemAvailable")
        if not total_kb or avail_kb is None:
            return {"mem_used_mb": None, "mem_total_mb": None, "mem_pct": None}
        used_kb = max(0, total_kb - avail_kb)
        return {
            # Against MemAvailable rather than MemFree, so the page cache is
            # not reported as memory pressure. A Pi with 3 GB of cache is fine.
            "mem_used_mb": round(used_kb / 1024),
            "mem_total_mb": round(total_kb / 1024),
            "mem_pct": round(100.0 * used_kb / total_kb, 1),
        }
