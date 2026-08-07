"""Session capture and read-back.

The property that matters is round-trip fidelity: a recording exists so a lap
can be pushed back through the DSP and compared against another parameter set.
If the read-back is not the packet that went in, every measurement built on top
of it is measuring the recorder.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from shaker.gt7.protocol import TelemetryPacket
from shaker.recording import SessionRecorder, list_sessions, read_session


def _packet(i: int) -> TelemetryPacket:
    """A packet whose every field is distinct, so a mix-up cannot pass."""
    return TelemetryPacket(
        packet_id=i,
        position_x=1.5 + i, position_y=-2.25, position_z=3.125,
        velocity_x=10.0, velocity_y=0.5, velocity_z=-1.0,
        ang_vel_x=0.01, ang_vel_y=0.25, ang_vel_z=-0.03,
        body_height=0.12, engine_rpm=6500.0 + i, speed_mps=55.5,
        wheel_rps_FL=170.1, wheel_rps_FR=170.2, wheel_rps_RL=171.3, wheel_rps_RR=171.4,
        tire_radius_FL=0.317, tire_radius_FR=0.318, tire_radius_RL=0.33, tire_radius_RR=0.331,
        suspension_FL=0.011, suspension_FR=0.012, suspension_RL=0.013, suspension_RR=0.014,
        lap_count=3, best_lap_ms=91234, last_lap_ms=92345,
        flags=0b11, car_code=2345, throttle=200, brake=30,
        current_gear=4, suggested_gear=5,
        min_alert_rpm=6000, max_alert_rpm=7200,
        boost_pressure=1.2, oil_pressure=5.5, water_temp=88.0, oil_temp=101.0,
        fuel_level=42.5, fuel_capacity=60.0,
        sway=1.25, heave=-0.5, surge=-9.81, has_motion=True,
    )


@pytest.fixture
def rec_dir(tmp_path: Path) -> Path:
    return tmp_path / "recordings"


def test_round_trip_preserves_every_packet_field(rec_dir: Path) -> None:
    r = SessionRecorder(rec_dir)
    path = r.start()
    sent = [_packet(i) for i in range(20)]
    for p in sent:
        r.on_packet(p)
    r.stop()

    header, packets = read_session(path)
    assert header["schema"] == 1
    got = list(packets)
    assert got == sent, "read-back differs from what was recorded"


def test_records_packets_the_app_would_reject(rec_dir: Path) -> None:
    """Menu and paused frames are kept deliberately.

    The gates that drop them live in AudioBus.push_packet and are code a replay
    should be able to exercise. A recording that pre-filtered could never test
    the filter.
    """
    r = SessionRecorder(rec_dir)
    path = r.start()
    r.on_packet(TelemetryPacket(packet_id=1, lap_count=-1))   # menu / replay
    r.on_packet(TelemetryPacket(packet_id=2, flags=0b10))     # paused
    r.stop()

    _, packets = read_session(path)
    assert [p.packet_id for p in packets] == [1, 2]


def test_two_clocks_are_recorded(rec_dir: Path) -> None:
    """packet_id for replay, wall-relative t for aligning other sensors."""
    r = SessionRecorder(rec_dir)
    path = r.start()
    for i in range(5):
        r.on_packet(_packet(i))
    r.stop()

    rows = [json.loads(line) for line in path.read_text().splitlines()[1:]]
    assert [row["packet_id"] for row in rows] == [0, 1, 2, 3, 4]
    times = [row["t"] for row in rows]
    assert all(t >= 0 for t in times)
    assert times == sorted(times), times


def test_size_cap_stops_recording(rec_dir: Path) -> None:
    """The SD card the Pi boots from is the one being written to."""
    r = SessionRecorder(rec_dir)
    path = r.start(max_bytes=2000)
    for i in range(500):
        r.on_packet(_packet(i))

    assert not r.recording
    assert r.status()["error"] == "size cap reached"
    assert path.stat().st_size < 20_000
    # Whatever was written must still be readable — a truncated tail would
    # make the cap a corruption bug rather than a limit.
    _, packets = read_session(path)
    assert len(list(packets)) > 0


def test_write_failure_disarms_instead_of_raising(rec_dir: Path) -> None:
    """Never raise into the packet path — that would take the rig down."""
    r = SessionRecorder(rec_dir)
    r.start()
    r._fh.close()  # simulate the underlying file going away

    r.on_packet(_packet(0))  # must not raise

    assert not r.recording
    assert r.status()["error"]


def test_packets_are_not_dropped_by_buffering(rec_dir: Path) -> None:
    """Flushing is periodic; stop() has to commit the remainder."""
    r = SessionRecorder(rec_dir)
    path = r.start()
    n = 137  # deliberately not a multiple of the flush interval
    for i in range(n):
        r.on_packet(_packet(i))
    r.stop()

    _, packets = read_session(path)
    assert len(list(packets)) == n


def test_restart_rolls_to_a_new_file(rec_dir: Path) -> None:
    r = SessionRecorder(rec_dir)
    first = r.start(name="one")
    r.on_packet(_packet(0))
    second = r.start(name="two")
    r.on_packet(_packet(1))
    r.stop()

    assert first != second
    assert first.exists() and second.exists()
    assert len(list(read_session(first)[1])) == 1
    assert len(list(read_session(second)[1])) == 1


def test_status_before_and_after(rec_dir: Path) -> None:
    r = SessionRecorder(rec_dir)
    assert r.status()["recording"] is False
    assert r.status()["packets"] == 0

    r.start()
    for i in range(3):
        r.on_packet(_packet(i))
    assert r.status()["recording"] is True
    assert r.status()["packets"] == 3

    stopped = r.stop()
    assert stopped["packets"] == 3
    # The response to "stop" must not claim it is still recording — the UI
    # drives its button straight off this field.
    assert stopped["recording"] is False
    assert stopped["seconds"] >= 0
    assert r.status()["recording"] is False


def test_on_packet_while_stopped_is_a_no_op(rec_dir: Path) -> None:
    r = SessionRecorder(rec_dir)
    r.on_packet(_packet(0))
    assert r.status()["packets"] == 0
    assert not rec_dir.exists()


def test_list_sessions_newest_first(rec_dir: Path) -> None:
    r = SessionRecorder(rec_dir)
    for name in ("alpha", "beta"):
        r.start(name=name)
        r.on_packet(_packet(0))
        r.stop()

    names = [s["name"] for s in list_sessions(rec_dir)]
    assert len(names) == 2
    assert names == sorted(names, reverse=True)
    assert all(s["bytes"] > 0 for s in list_sessions(rec_dir))


def test_names_are_slugged_into_the_filename(rec_dir: Path) -> None:
    r = SessionRecorder(rec_dir)
    path = r.start(name="Spa / GT3  wet!")
    r.stop()
    assert "/" not in path.name
    assert path.name.endswith(".jsonl")
    assert "Spa" in path.name


def test_read_session_rejects_a_headerless_file(tmp_path: Path) -> None:
    bad = tmp_path / "nope.jsonl"
    bad.write_text('{"packet_id": 1}\n')
    with pytest.raises(ValueError):
        read_session(bad)


def test_read_session_tolerates_unknown_keys(rec_dir: Path) -> None:
    """A recording must outlive a schema change, like a profile does."""
    r = SessionRecorder(rec_dir)
    path = r.start()
    r.on_packet(_packet(0))
    r.stop()

    lines = path.read_text().splitlines()
    row = json.loads(lines[1])
    row["field_from_the_future"] = 42
    path.write_text(lines[0] + "\n" + json.dumps(row) + "\n")

    _, packets = read_session(path)
    assert list(packets)[0].packet_id == 0


def test_float_rounding_stays_below_physical_significance(rec_dir: Path) -> None:
    """Rounding shrinks the file; it must not move a number that matters."""
    r = SessionRecorder(rec_dir)
    path = r.start()
    p = TelemetryPacket(position_x=123.456789012345, suspension_FL=0.0123456789)
    r.on_packet(p)
    r.stop()

    got = list(read_session(path)[1])[0]
    # Six decimals on metres is a micrometre.
    assert abs(got.position_x - p.position_x) < 1e-6
    assert abs(got.suspension_FL - p.suspension_FL) < 1e-6


def test_every_packet_field_survives_the_round_trip(rec_dir: Path) -> None:
    """Guards against a field being added to the packet but not the recording."""
    r = SessionRecorder(rec_dir)
    path = r.start()
    r.on_packet(_packet(0))
    r.stop()

    row = json.loads(path.read_text().splitlines()[1])
    for f in fields(TelemetryPacket):
        assert f.name in row, f"{f.name} is not being recorded"
