"""Limiter activity published for the diagnostics screen.

The limiter runs once per audio block and the UI polls twice a second, so the
property worth protecting is not accuracy but *visibility*: a reduction that
happens between two polls still has to show up. These tests drive the real
callback and assert on what a reader would have seen.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from shaker.audio.bus import AudioBus, TelemetryFeatures
from shaker.audio.stream import AudioOutput, _LIMIT_CEILING
from shaker.config import AudioConfig
from shaker.web.app import _limiter_diagnostics

FRAMES = 960


def _busy() -> TelemetryFeatures:
    """Corner entry with everything firing at once — what actually clips."""
    return TelemetryFeatures(
        speed_mps=55.0,
        suspension_activity=0.03,
        suspension_activity_front=0.03,
        suspension_activity_rear=0.02,
        engine_rpm=6500.0,
        engine_rpm_pct=0.98,
        throttle=200,
        brake=210,
        slip_magnitude=8.0,
        slip_front=-8.0,
        slip_rear=6.0,
    )


def _drive(cfg: AudioConfig, features: TelemetryFeatures, blocks: int = 200,
           muted: bool = False) -> AudioBus:
    bus = AudioBus(cfg)
    bus.features = features
    bus.muted = muted
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, out._out_channels), dtype=np.float32)
    for _ in range(blocks):
        out._callback(buf, FRAMES, None, None)
    return bus


def test_idle_rig_reports_no_limiting() -> None:
    """Nothing playing must read as zero, not as a limiter that never reset."""
    bus = _drive(AudioConfig(), TelemetryFeatures())
    d = _limiter_diagnostics(bus)
    assert d["reduction_db"] == 0.0
    assert d["peak_reduction_db"] == 0.0
    assert d["duty_pct"] == 0.0
    # Not merely equal to zero: -20*log10(1.0) is -0.0, which compares equal
    # here but reaches the browser intact and renders as "-0.0 dB".
    assert not any(math.copysign(1.0, d[k]) < 0 for k in d), d


def test_overdriven_rig_reports_reduction_and_duty() -> None:
    """The condition the panel exists to catch."""
    bus = _drive(AudioConfig(master_gain=4.0), _busy())
    d = _limiter_diagnostics(bus)
    assert d["peak_reduction_db"] > 3.0, d
    assert d["duty_pct"] > 50.0, d


def test_duty_rises_with_master_gain() -> None:
    """Monotonic in the control the readout is meant to inform."""
    duties = [
        _limiter_diagnostics(_drive(AudioConfig(master_gain=g), _busy()))["duty_pct"]
        for g in (0.25, 1.0, 4.0)
    ]
    assert duties[0] <= duties[1] <= duties[2], duties
    assert duties[0] < duties[2]


def test_peak_survives_a_transient_between_polls() -> None:
    """A brief reduction has to still be readable half a second later.

    Sampling the instantaneous gain is exactly what this panel must not do:
    one hard hit lasting a few blocks would be invisible at a 2 Hz poll.
    """
    cfg = AudioConfig(master_gain=4.0)
    bus = AudioBus(cfg)
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, out._out_channels), dtype=np.float32)

    for _ in range(10):
        out._callback(buf, FRAMES, None, None)
    hit = _limiter_diagnostics(bus)["peak_reduction_db"]
    assert hit > 3.0, hit

    # Go quiet and let half a second of blocks elapse — a full poll interval.
    bus.features = TelemetryFeatures()
    for _ in range(int(0.5 / (FRAMES / cfg.sample_rate))):
        out._callback(buf, FRAMES, None, None)

    after = _limiter_diagnostics(bus)
    # The instantaneous value has largely recovered — not to zero, because a
    # 0.25 s release is still audibly releasing half a second later.
    assert after["reduction_db"] < hit / 2, after
    # The hold has not, which is the entire point.
    assert after["peak_reduction_db"] > 2.0, after
    assert after["peak_reduction_db"] > after["reduction_db"], after


def test_peak_is_never_shallower_than_the_current_reduction() -> None:
    """A peak hold below the live value reads as a broken meter.

    The two fields are written on consecutive lines of the audio callback and
    read without a lock, so on a fast ramp the reader can sample one before and
    one after an update. Observed live on a rising mix at master_gain 6.0.
    """
    cfg = AudioConfig(master_gain=6.0)
    bus = AudioBus(cfg)
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, out._out_channels), dtype=np.float32)

    for _ in range(120):
        out._callback(buf, FRAMES, None, None)
        # Simulate the torn read directly: staleness in either field, in either
        # direction, must still come out as a coherent pair.
        for stale_hold in (bus.limit_hold, 1.0, 0.5):
            bus.limit_hold = stale_hold
            d = _limiter_diagnostics(bus)
            assert d["peak_reduction_db"] >= d["reduction_db"], d


def test_mute_is_not_reported_as_limiting() -> None:
    """The mute ramp is a 60 dB gain reduction and must not be counted as one.

    Muting a loud mix does briefly register, and correctly so: the first blocks
    of the fade are still a real signal being really limited. What must not
    happen is the reading *staying* pegged for as long as the rig is muted,
    which is what reading the gain after the mute ramp would produce.
    """
    bus = _drive(AudioConfig(master_gain=4.0), _busy(), muted=True)
    d = _limiter_diagnostics(bus)
    assert d["reduction_db"] == 0.0, d
    assert d["duty_pct"] < 5.0, d

    # And an unlimited mix that is merely muted never registers at all.
    quiet = _drive(AudioConfig(master_gain=0.1), _busy(), muted=True)
    assert _limiter_diagnostics(quiet) == {
        "reduction_db": 0.0,
        "peak_reduction_db": 0.0,
        "duty_pct": 0.0,
    }


def test_wiring_check_does_not_peg_the_readout() -> None:
    """The wiring pulse bypasses the limiter, so it must read as no reduction."""
    bus = AudioBus(AudioConfig(output_channels=2, master_gain=4.0))
    bus.features = _busy()
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, out._out_channels), dtype=np.float32)
    for _ in range(20):
        out._callback(buf, FRAMES, None, None)
    assert _limiter_diagnostics(bus)["reduction_db"] > 1.0

    bus.trigger_wiring_check(pulse_s=0.2, gap_s=0.1)
    for _ in range(10):
        out._callback(buf, FRAMES, None, None)
    assert _limiter_diagnostics(bus)["reduction_db"] == 0.0


def test_reduction_matches_the_gain_actually_applied() -> None:
    """The number is a measurement, not a mood: check it against the output.

    With the mix pinned well above the ceiling the limiter settles, so the
    reported reduction must equal the ratio between the peak the mix would
    have reached and the ceiling it was held to.
    """
    bus = _drive(AudioConfig(master_gain=4.0), _busy(), blocks=300)
    reported = _limiter_diagnostics(bus)["reduction_db"]
    implied_gain = 10.0 ** (-reported / 20.0)
    # The limiter targets ceiling/peak, so peak * gain lands on the ceiling.
    assert bus.limit_gain == pytest.approx(implied_gain, rel=1e-3)
    assert 0.0 < bus.limit_gain < 1.0
    assert bus.meter_front <= _LIMIT_CEILING + 1e-6
