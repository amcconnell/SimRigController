"""Host readings for the diagnostics screen.

These exist because a hot Pi throttles and a throttled Pi misses audio
callbacks, which arrives in the seat as intermittent glitching rather than as
an error anywhere. The properties worth protecting are that every reading is
optional — none of these paths exist on the development Mac — and that a
missing fan is not reported as a stopped one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shaker import system as sysmod
from shaker.system import SystemStats


@pytest.fixture
def fake_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def install(**files: str) -> Path:
        for name, text in files.items():
            (tmp_path / name).write_text(text)
        monkeypatch.setattr(sysmod, "_TEMP_PATH", tmp_path / "temp")
        monkeypatch.setattr(sysmod, "_STAT_PATH", tmp_path / "stat")
        monkeypatch.setattr(sysmod, "_MEMINFO_PATH", tmp_path / "meminfo")
        monkeypatch.setattr(sysmod, "_LOADAVG_PATH", tmp_path / "loadavg")
        return tmp_path
    return install


def test_everything_missing_reads_as_none_not_an_error() -> None:
    """The development Mac has none of these paths."""
    s = SystemStats(fan_state_path=Path("/nonexistent/fan"))
    got = s.read()
    for key in ("cpu_temp_c", "fan_duty_pct", "cpu_pct", "load_1m",
                "mem_used_mb", "mem_total_mb", "mem_pct"):
        assert got[key] is None, key


def test_temperature_is_millidegrees(fake_proc) -> None:
    fake_proc(temp="38900\n")
    assert SystemStats().cpu_temp_c() == 38.9


def test_garbage_temperature_is_none(fake_proc) -> None:
    fake_proc(temp="not a number\n")
    assert SystemStats().cpu_temp_c() is None


def test_absent_fan_is_none_not_zero(tmp_path: Path) -> None:
    """An unmanaged fan on this hardware runs flat out, so 0 would be backwards."""
    assert SystemStats(fan_state_path=tmp_path / "missing").fan_duty_pct() is None


def test_fan_duty_is_read_and_clamped(tmp_path: Path) -> None:
    f = tmp_path / "fan"
    f.write_text("35\n")
    assert SystemStats(fan_state_path=f).fan_duty_pct() == 35
    f.write_text("400\n")
    assert SystemStats(fan_state_path=f).fan_duty_pct() == 100
    f.write_text("junk")
    assert SystemStats(fan_state_path=f).fan_duty_pct() is None


def test_cpu_needs_two_samples_and_then_reports_a_rate(fake_proc) -> None:
    """A percentage is a rate, so the first call has nothing to compare to."""
    d = fake_proc(stat="cpu  100 0 100 800 0 0 0 0 0 0\n")
    s = SystemStats()
    assert s.cpu_pct() is None, "first call cannot know a rate"

    # 100 more busy ticks, 300 more total: 33.3% over the interval.
    (d / "stat").write_text("cpu  200 0 100 1000 0 0 0 0 0 0\n")
    assert s.cpu_pct() == pytest.approx(33.3, abs=0.2)


def test_iowait_counts_as_idle_not_busy(fake_proc) -> None:
    """A slow SD card must not read as a CPU problem."""
    d = fake_proc(stat="cpu  0 0 0 1000 0 0 0 0 0 0\n")
    s = SystemStats()
    s.cpu_pct()
    (d / "stat").write_text("cpu  0 0 0 1000 1000 0 0 0 0 0\n")   # all iowait
    assert s.cpu_pct() == pytest.approx(0.0, abs=0.1)


def test_counter_reset_is_reported_as_unknown(fake_proc) -> None:
    d = fake_proc(stat="cpu  500 0 500 5000 0 0 0 0 0 0\n")
    s = SystemStats()
    s.cpu_pct()
    (d / "stat").write_text("cpu  1 0 1 10 0 0 0 0 0 0\n")   # counters went backwards
    assert s.cpu_pct() is None


def test_memory_uses_available_not_free(fake_proc) -> None:
    """Page cache is not memory pressure — a Pi with GBs of cache is healthy."""
    fake_proc(meminfo=(
        "MemTotal:        4194304 kB\n"
        "MemFree:          104857 kB\n"
        "MemAvailable:    3145728 kB\n"
        "Buffers:          200000 kB\n"
    ))
    m = SystemStats().memory()
    assert m["mem_total_mb"] == 4096
    assert m["mem_used_mb"] == 1024      # total - available, not total - free
    assert m["mem_pct"] == pytest.approx(25.0, abs=0.1)


def test_meminfo_without_available_is_none(fake_proc) -> None:
    fake_proc(meminfo="MemTotal: 4194304 kB\nMemFree: 104857 kB\n")
    assert SystemStats().memory()["mem_pct"] is None


def test_load_average_is_the_one_minute_figure(fake_proc) -> None:
    fake_proc(loadavg="0.42 0.31 0.28 1/234 5678\n")
    assert SystemStats().load_1m() == 0.42


def test_read_is_json_safe(fake_proc) -> None:
    import json
    fake_proc(temp="45000\n", stat="cpu  1 2 3 4 5 6 7 8 9 10\n",
              meminfo="MemTotal: 100 kB\nMemAvailable: 50 kB\n",
              loadavg="0.1 0.2 0.3 1/2 3\n")
    text = json.dumps(SystemStats().read())
    assert "Infinity" not in text and "NaN" not in text


def test_fan_path_is_resolved_at_call_time(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """The default must follow the module constant, not a value bound at import.

    create_app builds SystemStats with no argument, so anything pointing the
    constant elsewhere — a test, a stub rig — has to be honoured.
    """
    f = tmp_path / "duty"
    f.write_text("42\n")
    monkeypatch.setattr(sysmod, "FAN_STATE_PATH", f)
    assert SystemStats().fan_duty_pct() == 42
