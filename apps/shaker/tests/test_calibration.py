"""Per-pod sensitivity calibration against gravity.

The ADXL345 is specified at 256 LSB/g with a 230-282 spread, so two parts can
legitimately disagree by ten percent. That is invisible almost everywhere and
decisive in the crosstalk figure, which divides one pod's reading by the
other's. The things worth protecting here are that a calibration only happens
when the pod is genuinely still, and that an implausible reading is refused
rather than quietly divided away.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from shaker.sensors.adxl345 import ADDR_ALT, ADDR_PRIMARY
from shaker.sensors.calibration import calibrate
from shaker.sensors.pods import Pod, SensorHub

from test_sensors import FakeBus

_FAST = dict(window_s=0.25, interval_s=0.05)


def _hub_reading(front_g: float, rear_g: float | None = None,
                 shake_g: float = 0.0) -> tuple[SensorHub, FakeBus, callable]:
    """A hub whose pods report a chosen gravity magnitude.

    `front_g` is what the part *reports* for a true 1 g — so 0.9 models a
    sensor reading nine percent low.
    """
    addrs = (ADDR_PRIMARY,) if rear_g is None else (ADDR_PRIMARY, ADDR_ALT)
    bus = FakeBus(addresses=addrs)
    hub = SensorHub()
    hub.start(bus=bus)

    import math
    import threading
    stop = threading.Event()

    def feed():
        n = 0
        while not stop.is_set():
            for _ in range(16):
                ph = 2 * math.pi * 50.0 * n / 800.0
                wob = shake_g * math.sin(ph)
                bus.push(ADDR_PRIMARY, 0.0, 0.0, front_g + wob)
                if rear_g is not None:
                    bus.push(ADDR_ALT, 0.0, 0.0, rear_g + wob)
                n += 1
            time.sleep(0.02)

    t = threading.Thread(target=feed, daemon=True)
    t.start()

    deadline = time.monotonic() + 3.0
    want = len(addrs)
    while time.monotonic() < deadline and sum(p.stats.present for p in hub._pods) < want:
        time.sleep(0.02)
    time.sleep(0.5)   # let the gravity tracker settle on the reported value

    def teardown() -> None:
        stop.set()
        hub.stop()

    return (hub, bus, teardown)


def test_scale_corrects_a_low_reading_pod() -> None:
    hub, _, done = _hub_reading(front_g=0.90)
    try:
        run = asyncio.run(calibrate(hub, **_FAST))
        r = run.results[0]
        assert r.ok, r.reason
        assert r.measured_g == pytest.approx(0.90, abs=0.02)
        assert r.scale == pytest.approx(1 / 0.90, rel=0.03)
        assert r.error_pct == pytest.approx(-10.0, abs=2.0)
    finally:
        done()


def test_a_calibrated_pod_then_reads_one_g() -> None:
    """The point of the exercise, checked end to end through the real filters."""
    hub, _, done = _hub_reading(front_g=0.90)
    try:
        assert asyncio.run(calibrate(hub, **_FAST)).results[0].ok
        # Applying a scale re-seeds gravity, so one poll is enough — if it
        # did not, this would take about seven seconds to converge.
        time.sleep(0.3)
        assert hub._pods[0].stats.tilt_g == pytest.approx(1.0, abs=0.03)
    finally:
        done()


def test_recalibrating_converges_rather_than_double_correcting() -> None:
    """Scale composes with the existing one, so a second press is a no-op."""
    hub, _, done = _hub_reading(front_g=0.90)
    try:
        first = asyncio.run(calibrate(hub, **_FAST)).results[0].scale
        time.sleep(0.3)
        second = asyncio.run(calibrate(hub, **_FAST)).results[0]
        assert second.ok, second.reason
        assert second.scale == pytest.approx(first, rel=0.05), "must not compound"
    finally:
        done()


def test_a_moving_pod_is_refused() -> None:
    """Calibrating while holding it would bake the hand into every later number."""
    hub, bus, done = _hub_reading(front_g=1.0)
    try:
        import threading

        stop = threading.Event()

        def tilt():
            # Swing the pod between two orientations, so the gravity magnitude
            # estimate wanders — exactly what a hand does.
            g = 1.0
            while not stop.is_set():
                g = 0.6 if g > 0.8 else 1.0
                for _ in range(160):
                    bus.push(ADDR_PRIMARY, 0.0, 0.0, g)
                time.sleep(0.05)

        t = threading.Thread(target=tilt, daemon=True)
        t.start()
        time.sleep(0.4)
        run = asyncio.run(calibrate(hub, **_FAST))
        stop.set()

        r = run.results[0]
        # Either net is a pass — being picked up shows up mostly as AC, because
        # the sample moves before the 1.5 s gravity estimate follows it.
        assert not r.ok
        assert any(k in (r.reason or "") for k in ("moved", "shaken", "too far from 1 g")), r.reason
        assert hub._pods[0].scale == 1.0, "a refused calibration must change nothing"
    finally:
        done()


def test_an_implausible_reading_is_refused_not_divided_away() -> None:
    """0.3 g at rest is a fault, not a part to trim.

    Silently scaling it by 3.3 would make every later measurement look sane
    while hiding a wrong range or a bad mount.
    """
    hub, _, done = _hub_reading(front_g=0.30)
    try:
        r = asyncio.run(calibrate(hub, **_FAST)).results[0]
        assert not r.ok
        assert "too far from 1 g" in (r.reason or ""), r.reason
        assert hub._pods[0].scale == 1.0, "an unusable reading must not be applied"
    finally:
        done()


def test_both_pods_are_calibrated_independently() -> None:
    hub, _, done = _hub_reading(front_g=0.90, rear_g=1.08)
    try:
        run = asyncio.run(calibrate(hub, **_FAST))
        assert run.as_dict()["ok"], run.as_dict()
        by = {r.pod: r for r in run.results}
        assert by["front"].scale == pytest.approx(1 / 0.90, rel=0.04)
        assert by["rear"].scale == pytest.approx(1 / 1.08, rel=0.04)
        # Independent errors, independently removed — the point of the feature.
        assert by["front"].scale != by["rear"].scale
    finally:
        done()


def test_only_the_named_pod_is_touched() -> None:
    hub, _, done = _hub_reading(front_g=0.90, rear_g=1.08)
    try:
        run = asyncio.run(calibrate(hub, pod_name="rear", **_FAST))
        assert [r.pod for r in run.results] == ["rear"]
        assert hub._pods[0].scale == 1.0
        assert hub._pods[1].scale != 1.0
    finally:
        done()


def test_absent_pods_are_skipped_not_failed() -> None:
    """A one-pod rig is a legitimate configuration, not an error."""
    hub, _, done = _hub_reading(front_g=0.95)
    try:
        run = asyncio.run(calibrate(hub, **_FAST))
        assert [r.pod for r in run.results] == ["front"]
        assert run.results[0].ok
    finally:
        done()


def test_no_pods_at_all_reports_a_reason() -> None:
    hub = SensorHub()
    hub.start(bus=FakeBus(addresses=()))
    try:
        run = asyncio.run(calibrate(hub, **_FAST))
        assert not run.as_dict()["ok"]
        assert "detected" in (run.results[0].reason or "")
    finally:
        hub.stop()


def test_unknown_pod_name_is_rejected() -> None:
    hub = SensorHub()
    hub.start(bus=FakeBus(addresses=()))
    try:
        run = asyncio.run(calibrate(hub, pod_name="middle", **_FAST))
        assert run.results[0].reason == "no such pod"
    finally:
        hub.stop()


def test_scale_reaches_the_reported_status() -> None:
    hub, _, done = _hub_reading(front_g=0.90)
    try:
        asyncio.run(calibrate(hub, **_FAST))
        assert hub.status()["pods"][0]["scale"] == pytest.approx(1 / 0.90, rel=0.04)
    finally:
        done()


def test_ambient_vibration_does_not_block_calibration() -> None:
    """A bench hums. Only movement corrupts a DC estimate, not shake."""
    hub, _, done = _hub_reading(front_g=0.92, shake_g=0.04)
    try:
        r = asyncio.run(calibrate(hub, **_FAST)).results[0]
        assert r.ok, r.reason
        assert r.vibration_g > 0.0
    finally:
        done()


def test_pod_scale_is_applied_to_raw_samples() -> None:
    """Unit-level: the correction lands before the filters, not after."""
    bus = FakeBus()
    pod = Pod("front", ADDR_PRIMARY, 800.0, 16, scale=2.0)
    pod.attach(bus)
    bus.push(ADDR_PRIMARY, 0.0, 0.0, 0.5, n=32)
    pod.poll(0.02)
    assert pod.stats.z == pytest.approx(1.0, abs=0.01)
    assert pod.stats.tilt_g == pytest.approx(1.0, abs=0.01)
