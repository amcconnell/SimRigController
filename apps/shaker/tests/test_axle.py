"""Per-axle (front / rear) derived features and the /api/status block.

These values are groundwork for routing audio to two shakers — a pedal-deck
one and a seat one. Nothing on the audio path reads them yet, so the load
bearing test in this file is the regression one: the legacy whole-car scalars
that *do* drive audio must come out bit-identical to how they were computed
before the split existed.

They are also the first consumers of the FL/FR/RL/RR corner labels that are not
an order-invariant max(), so the single-axle-hit tests below are what would
catch a permuted corner mapping in protocol.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from shaker.audio.bus import (
    _ACTIVITY_ATTACK,
    _ACTIVITY_DECAY,
    _HPF_ALPHA,
    AudioBus,
)
from shaker.config import AudioConfig, Config, GT7Config
from shaker.gt7.client import GT7Client
from shaker.gt7.protocol import TelemetryPacket
from shaker.web.app import create_app

_FRONTEND_TYPES = Path(__file__).resolve().parents[1] / "frontend" / "src" / "types" / "config.ts"


def _active_packet() -> TelemetryPacket:
    """A packet that passes the bus's "is driving" filter."""
    p = TelemetryPacket()
    p.flags = 0b01  # on_track bit set, not paused
    p.lap_count = 1
    return p


def _rolling_packet(frame: int, speed_mps: float = 30.0) -> TelemetryPacket:
    """An on-track packet rolling at `speed_mps` with all four wheels tracking.

    Position advances every frame so the frozen-payload gate (which resets
    features after 30 identical frames) never fires during a test sequence.
    Wheel "rps" is set so rps * radius == speed exactly, i.e. zero slip.
    """
    p = _active_packet()
    p.position_x = 100.0 + frame * 0.8
    p.speed_mps = speed_mps
    for corner in ("FL", "FR", "RL", "RR"):
        setattr(p, f"tire_radius_{corner}", 0.3)
        setattr(p, f"wheel_rps_{corner}", speed_mps / 0.3)
    return p


# --- Suspension: front vs rear ------------------------------------------------
# A permuted corner mapping is undetectable in `suspension_activity` because it
# is a max() over all four. Splitting by axle is the first time the labels are
# load bearing, so these are the tests that would catch it.


def test_front_only_hit_raises_front_activity_and_leaves_rear_quiet() -> None:
    bus = AudioBus(AudioConfig())
    for frame in range(60):  # settle: constant load high-passes away to nothing
        p = _rolling_packet(frame)
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.05
        bus.push_packet(p)
    assert bus.features.suspension_activity_front < 1e-3
    assert bus.features.suspension_activity_rear < 1e-3

    hit = _rolling_packet(60)
    hit.suspension_FL = 0.30  # kerb under the left front only
    hit.suspension_FR = hit.suspension_RL = hit.suspension_RR = 0.05
    bus.push_packet(hit)

    assert bus.features.suspension_activity_front > 0.05
    assert bus.features.suspension_activity_rear < 1e-3


def test_rear_only_hit_raises_rear_activity_and_leaves_front_quiet() -> None:
    bus = AudioBus(AudioConfig())
    for frame in range(60):
        p = _rolling_packet(frame)
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.05
        bus.push_packet(p)

    hit = _rolling_packet(60)
    hit.suspension_RR = 0.30  # same kerb, now under the right rear
    hit.suspension_FL = hit.suspension_FR = hit.suspension_RL = 0.05
    bus.push_packet(hit)

    assert bus.features.suspension_activity_rear > 0.05
    assert bus.features.suspension_activity_front < 1e-3


def test_axle_envelopes_cannot_be_rebuilt_from_each_other() -> None:
    """Why each axle needs its own envelope state rather than a max() of two.

    On alternating single-corner hits (a ripple strip) the whole-car envelope
    is re-attacked twice as often as either axle's, while each axle's decays
    through the frames its own corners are quiet. max(front, rear) settles
    around 84% of the whole-car value here — so rebuilding the scalar the audio
    path reads out of the two axle envelopes would quietly turn every ripple
    strip down.
    """
    bus = AudioBus(AudioConfig())
    for frame in range(60):
        p = _rolling_packet(frame)
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.05
        if frame % 4 == 0:
            p.suspension_FL = 0.30  # left front clips the strip
        elif frame % 4 == 2:
            p.suspension_RR = 0.30  # right rear follows it over
        bus.push_packet(p)

    f = bus.features
    rebuilt = max(f.suspension_activity_front, f.suspension_activity_rear)
    assert f.suspension_activity > 0.05  # the sequence really is exciting it
    assert rebuilt < 0.9 * f.suspension_activity


# --- Slip: signed, per axle ---------------------------------------------------


def test_slip_front_is_positive_when_a_front_wheel_spins() -> None:
    bus = AudioBus(AudioConfig())
    p = _rolling_packet(0, speed_mps=30.0)
    p.wheel_rps_FR = 130.0  # 39 m/s surface speed against a 30 m/s car
    bus.push_packet(p)

    assert bus.features.slip_front == pytest.approx(9.0, abs=1e-3)
    assert bus.features.slip_rear == pytest.approx(0.0, abs=1e-6)


def test_slip_rear_is_negative_when_a_rear_wheel_locks() -> None:
    bus = AudioBus(AudioConfig())
    p = _rolling_packet(0, speed_mps=30.0)
    p.wheel_rps_RL = 0.0  # fully locked: wheel stopped, car still at 30 m/s
    bus.push_packet(p)

    assert bus.features.slip_rear == pytest.approx(-30.0, abs=1e-3)
    assert bus.features.slip_front == pytest.approx(0.0, abs=1e-6)
    # The legacy scalar sees the same event with the sign destroyed, which is
    # exactly why it cannot tell a lockup from a 30 m/s wheelspin.
    assert bus.features.slip_magnitude == pytest.approx(30.0, abs=1e-3)


def test_axle_slip_keeps_the_sign_of_the_dominant_corner() -> None:
    """max(pair, key=abs), not max(abs(pair)): a mild spin on one wheel must not
    hide (or flip the sign of) a heavy lockup on the other."""
    bus = AudioBus(AudioConfig())
    p = _rolling_packet(0, speed_mps=30.0)
    p.wheel_rps_FL = 31.0 / 0.3  # +1.0 m/s, spinning slightly
    p.wheel_rps_FR = 24.0 / 0.3  # -6.0 m/s, locking hard
    bus.push_packet(p)

    assert bus.features.slip_front == pytest.approx(-6.0, abs=1e-3)


def test_axle_slip_is_zero_when_every_wheel_tracks_the_car() -> None:
    bus = AudioBus(AudioConfig())
    bus.push_packet(_rolling_packet(0, speed_mps=45.0))
    assert bus.features.slip_front == pytest.approx(0.0, abs=1e-6)
    assert bus.features.slip_rear == pytest.approx(0.0, abs=1e-6)


# --- Regression: the audio path must not have moved ---------------------------


class _RefHPF:
    """Independent restatement of the one-pole HPF, for the reference below."""

    def __init__(self) -> None:
        self.last_in = 0.0
        self.last_out = 0.0

    def step(self, x: float) -> float:
        y = _HPF_ALPHA * (self.last_out + x - self.last_in)
        self.last_in, self.last_out = x, y
        return y


def _legacy_scalars(packets: list[TelemetryPacket]) -> list[tuple[float, float]]:
    """(suspension_activity, slip_magnitude) computed the pre-split way.

    Written out longhand rather than called through the bus: the point is to
    pin the *old* algorithm — one whole-car envelope over a four-corner max,
    and an unsigned four-corner max slip — so that a change in how the bus
    reduces corners shows up here as a diff.
    """
    hpf = {c: _RefHPF() for c in ("FL", "FR", "RL", "RR")}
    activity = 0.0
    out = []
    for p in packets:
        bump = max(
            abs(hpf["FL"].step(p.suspension_FL)),
            abs(hpf["FR"].step(p.suspension_FR)),
            abs(hpf["RL"].step(p.suspension_RL)),
            abs(hpf["RR"].step(p.suspension_RR)),
        )
        if bump > activity:
            activity = _ACTIVITY_ATTACK * bump + (1 - _ACTIVITY_ATTACK) * activity
        else:
            activity = _ACTIVITY_DECAY * activity
        slip = max(
            abs(p.wheel_rps_FL * p.tire_radius_FL - p.speed_mps),
            abs(p.wheel_rps_FR * p.tire_radius_FR - p.speed_mps),
            abs(p.wheel_rps_RL * p.tire_radius_RL - p.speed_mps),
            abs(p.wheel_rps_RR * p.tire_radius_RR - p.speed_mps),
        )
        out.append((activity, slip))
    return out


def _varied_lap() -> list[TelemetryPacket]:
    """A short stint: smooth tarmac, a ripple strip, a big compression, a lockup.

    The alternating single-corner section is the part that matters. The
    envelope is asymmetric (attack 0.4, decay 0.92 per packet), so it does not
    commute with the per-axle max — if the whole-car value were ever rebuilt
    from the two axle envelopes, this is the input that would expose it.
    """
    packets = []
    for frame in range(90):
        p = _rolling_packet(frame, speed_mps=40.0 + frame * 0.05)
        p.suspension_FL = p.suspension_FR = p.suspension_RL = p.suspension_RR = 0.05

        if 20 <= frame < 50:  # ripple strip: FL, then RR, then FL...
            if frame % 2 == 0:
                p.suspension_FL = 0.22
            else:
                p.suspension_RR = 0.24
        elif 50 <= frame < 56:  # all four compress over a crest
            p.suspension_FL = p.suspension_FR = 0.18
            p.suspension_RL = p.suspension_RR = 0.20
        elif 60 <= frame < 70:  # braking: fronts locking, one rear spinning
            p.wheel_rps_FL = 20.0 / 0.3
            p.wheel_rps_FR = 12.0 / 0.3
            p.wheel_rps_RR = 55.0 / 0.3
        packets.append(p)
    return packets


def test_legacy_scalars_are_unchanged_by_the_axle_split() -> None:
    """The one property this whole change must not break: identical audio.

    `suspension_activity` and `slip_magnitude` are what the audio callback
    reads, so they are compared exactly (not approx) against the old algorithm
    on every frame of a varied stint.
    """
    packets = _varied_lap()
    expected = _legacy_scalars(packets)

    bus = AudioBus(AudioConfig())
    for frame, (p, (want_activity, want_slip)) in enumerate(zip(packets, expected)):
        bus.push_packet(p)
        assert bus.features.suspension_activity == want_activity, f"frame {frame}"
        assert bus.features.slip_magnitude == want_slip, f"frame {frame}"

    # And the stint really did exercise the interesting parts, rather than
    # passing because everything stayed at zero.
    assert max(a for a, _ in expected) > 0.05
    assert max(s for _, s in expected) > 5.0


# --- Reset --------------------------------------------------------------------


def test_reset_features_clears_the_axle_fields() -> None:
    bus = AudioBus(AudioConfig())
    for frame in range(10):
        p = _rolling_packet(frame, speed_mps=30.0)
        p.suspension_FL = 0.2 if frame % 2 else 0.05
        p.suspension_RR = 0.2 if frame % 2 else 0.05
        p.wheel_rps_FL = 130.0
        bus.push_packet(p)
    assert bus.features.suspension_activity_front > 0.0
    assert bus.features.slip_front != 0.0

    bus.reset_features()
    f = bus.features
    assert f.suspension_activity_front == 0.0
    assert f.suspension_activity_rear == 0.0
    assert f.slip_front == 0.0
    assert f.slip_rear == 0.0


def test_reset_clears_the_axle_envelope_accumulators_not_just_the_snapshot() -> None:
    """The envelopes live outside `features`, so zeroing the dataclass is not
    enough — a stale accumulator would decay into the next session's values."""
    bus = AudioBus(AudioConfig())
    for frame in range(10):
        p = _rolling_packet(frame)
        p.suspension_FL = 0.3 if frame % 2 else 0.05
        bus.push_packet(p)
    assert bus.features.suspension_activity_front > 0.0

    bus.reset_features()
    # One quiet frame after the reset: with the accumulator cleared this is
    # driven only by the (also reset) HPF, so it stays at zero.
    quiet = _rolling_packet(100)
    bus.push_packet(quiet)
    assert bus.features.suspension_activity_front == 0.0


# --- /api/status --------------------------------------------------------------


def _status_payload(packet: TelemetryPacket | None, bus: AudioBus) -> dict[str, Any]:
    """Call the real /api/status handler.

    No TestClient: starlette's needs httpx, which is not a dependency here. The
    handler is a plain sync function, so pulling it off the route table
    exercises the same code an HTTP request would.
    """
    gt7 = GT7Client(GT7Config())
    gt7.latest_packet = packet
    app = create_app(lambda: Config(), lambda _cfg: None, gt7, bus)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/status")
    return endpoint()


def test_status_exposes_the_axle_block_with_raw_corner_values() -> None:
    bus = AudioBus(AudioConfig())
    p = _rolling_packet(0, speed_mps=30.0)
    p.wheel_rps_RR = 130.0  # 39 m/s: right rear spinning
    p.suspension_FL = 0.07
    p.current_gear = 4
    bus.push_packet(p)

    axle = _status_payload(p, bus)["axle"]

    assert axle["slip_rear"] == pytest.approx(9.0, abs=1e-3)
    assert axle["slip_front"] == pytest.approx(0.0, abs=1e-6)
    assert axle["slip_magnitude"] == pytest.approx(9.0, abs=1e-3)
    assert "suspension_activity_front" in axle
    assert "suspension_activity_rear" in axle
    assert "suspension_activity" in axle

    raw = axle["raw"]
    assert raw["speed_mps"] == pytest.approx(30.0)
    assert raw["current_gear"] == 4
    assert raw["suspension_FL"] == pytest.approx(0.07)
    assert raw["wheel_rps_RR"] == pytest.approx(130.0)
    assert raw["tire_radius_RR"] == pytest.approx(0.3)
    # The comparison that settles the wheel_rps units question: surface speed
    # against vehicle speed on a wheel that is tracking.
    assert raw["wheel_surface_speed_FL"] == pytest.approx(30.0, abs=1e-3)
    assert raw["wheel_surface_speed_RR"] == pytest.approx(39.0, abs=1e-3)


def test_status_axle_block_present_before_the_first_packet() -> None:
    """The UI polls status from startup, well before a PS5 is found."""
    payload = _status_payload(None, AudioBus(AudioConfig()))
    assert payload["telemetry"] is None
    axle = payload["axle"]
    assert axle["raw"] is None
    assert axle["slip_front"] == 0.0
    assert axle["slip_rear"] == 0.0
    assert axle["suspension_activity_front"] == 0.0
    assert axle["suspension_activity_rear"] == 0.0


def test_typescript_axle_interfaces_match_the_status_payload() -> None:
    """The axle block is hand-mirrored into TypeScript; nothing else catches drift.

    Same failure mode as the AudioConfig mirror in test_config.py — a renamed
    or added key fails silently at runtime as `undefined` in the readout.
    """
    bus = AudioBus(AudioConfig())
    bus.push_packet(_rolling_packet(0))
    axle = _status_payload(_rolling_packet(0), bus)["axle"]

    types_ts = _FRONTEND_TYPES.read_text()
    for interface, expected in (
        ("AxleStatus", set(axle)),
        ("AxleRawStatus", set(axle["raw"])),
    ):
        body = re.search(rf"export interface {interface} \{{(.*?)\n\}}", types_ts, re.S)
        assert body, f"could not find the {interface} interface"
        assert set(re.findall(r"^\s*(\w+)\??:", body.group(1), re.M)) == expected, interface


# --- Body motion diagnostics -------------------------------------------------


def test_motion_fields_need_the_longer_packet() -> None:
    """sway/heave/surge live past the base layout. GT7 locks the format to
    whichever heartbeat it sees first, so a session where another tool got in
    first serves the short packet and these are simply absent — which must
    read as "unavailable", not as zeros that look like a stationary car."""
    import struct

    from shaker.gt7.protocol import parse_packet

    short = bytearray(0x128)
    short[0:4] = b"0S7G"
    assert parse_packet(bytes(short)).has_motion is False

    long_ = bytearray(0x158)
    long_[0:4] = b"0S7G"
    struct.pack_into("<3f", long_, 0x130, 1.5, -2.5, 3.5)
    p = parse_packet(bytes(long_))
    assert p.has_motion is True
    assert (p.sway, p.heave, p.surge) == (1.5, -2.5, 3.5)


def test_reference_accelerations_are_derived_correctly() -> None:
    """The references are the experiment: sway/heave/surge mean nothing until
    compared against quantities whose meaning is established."""
    from shaker.audio.bus import AudioBus
    from shaker.config import AudioConfig
    from shaker.gt7.protocol import TelemetryPacket

    bus = AudioBus(AudioConfig())
    for i in range(60):
        p = TelemetryPacket()
        p.flags, p.lap_count, p.packet_id = 0b01, 1, i
        p.speed_mps = 20.0 + i * 0.05      # +3.0 m/s^2 at 60 Hz
        p.ang_vel_y = 0.3
        p.position_x = i * 1.0
        bus.push_packet(p)

    assert bus.features.long_accel == pytest.approx(3.0, abs=0.05)
    assert bus.features.lat_accel == pytest.approx((20.0 + 59 * 0.05) * 0.3, rel=1e-3)


def test_packet_id_gap_does_not_produce_a_phantom_spike() -> None:
    """UDP drops. Differentiating against wall time would read a dropped frame
    as a huge acceleration; packet_id makes the gap visible instead."""
    from shaker.audio.bus import AudioBus
    from shaker.config import AudioConfig
    from shaker.gt7.protocol import TelemetryPacket

    bus = AudioBus(AudioConfig())
    for pid, speed in ((0, 20.0), (1, 20.05), (2, 20.10)):
        p = TelemetryPacket()
        p.flags, p.lap_count, p.packet_id = 0b01, 1, pid
        p.speed_mps, p.position_x = speed, pid * 1.0
        bus.push_packet(p)
    steady = bus.features.long_accel

    p = TelemetryPacket()                  # 3-frame gap, 3x the speed change
    p.flags, p.lap_count, p.packet_id = 0b01, 1, 5
    p.speed_mps, p.position_x = 20.25, 9.0
    bus.push_packet(p)
    assert bus.features.long_accel == pytest.approx(steady, abs=0.6)
