"""Pod watermark interrupts, and the guarantee that they cannot break sampling.

The interrupt is an optimisation on latency and nothing else — the FIFO already
decouples when the Pi reads from when the part sampled. So the property worth
protecting is not that edges arrive, it is that sampling is unaffected when
they do not: a broken INT trace should cost a few milliseconds of latency, not
a rig that silently stops measuring while still reporting both pods present.
"""

from __future__ import annotations

import threading
import time

import pytest

from shaker.sensors.adxl345 import (
    ADDR_PRIMARY,
    ADXL345,
    INT_WATERMARK,
    _REG_FIFO_CTL,
    _REG_INT_ENABLE,
    _REG_INT_MAP,
)
from shaker.sensors.interrupts import EdgeWatcher
from shaker.sensors.pods import SensorHub

from test_sensors import FakeBus


def _writes(fake: FakeBus, reg: int) -> list[int]:
    return [v for _, r, v in fake.writes if r == reg]


# --- the part --------------------------------------------------------------


def test_a_watermark_enables_the_interrupt_and_maps_it_to_int1() -> None:
    fake = FakeBus()
    ADXL345(fake, ADDR_PRIMARY).configure(watermark=INT_WATERMARK)
    assert _writes(fake, _REG_INT_MAP) == [0x00], "watermark must land on INT1"
    # Disabled while reconfiguring, then enabled — the order matters, because an
    # enabled source during the FIFO reset asserts against a doomed buffer.
    assert _writes(fake, _REG_INT_ENABLE) == [0x00, 0x02]


def test_no_watermark_leaves_interrupts_off() -> None:
    """The arrangement before the board existed, and the one a bare Pi gets."""
    fake = FakeBus()
    ADXL345(fake, ADDR_PRIMARY).configure()
    assert _writes(fake, _REG_INT_ENABLE) == [0x00]
    assert _writes(fake, _REG_INT_MAP) == []


def test_the_watermark_reaches_the_fifo_register() -> None:
    fake = FakeBus()
    ADXL345(fake, ADDR_PRIMARY).configure(watermark=16)
    # Bypass (0x00) then stream (0x80) | samples.
    assert _writes(fake, _REG_FIFO_CTL) == [0x00, 0x80 | 16]


def test_a_watermark_of_31_is_not_the_default_for_interrupts() -> None:
    """31 fires only once the FIFO is full — 39 ms at 800 Hz, no margin left."""
    assert INT_WATERMARK < 31
    assert INT_WATERMARK * (1 / 800.0) < 0.025


@pytest.mark.parametrize("bad", [0, 32, -1, 99])
def test_an_out_of_range_watermark_is_refused(bad: int) -> None:
    with pytest.raises(ValueError):
        ADXL345(FakeBus(), ADDR_PRIMARY).configure(watermark=bad)


# --- the watcher -----------------------------------------------------------


def test_no_gpio_device_is_a_state_not_a_failure() -> None:
    """Developed on a Mac: the library imports fine, /dev/gpiochip0 does not exist."""
    w = EdgeWatcher([4, 17], threading.Event())
    w.start()
    s = w.status()
    assert s["available"] is False
    assert s["error"]
    w.stop()


def test_no_pins_configured_is_also_a_state() -> None:
    w = EdgeWatcher([0, 0], threading.Event())
    w.start()
    assert w.status()["available"] is False
    w.stop()


class FakeReadyLine:
    def __init__(self) -> None:
        self.consumed = 0

    def read_event(self) -> None:
        self.consumed += 1


def test_an_edge_sets_the_event_and_is_consumed() -> None:
    """An unconsumed edge leaves the pin readable forever, so the poll spins."""
    wake = threading.Event()
    w = EdgeWatcher([4], wake)
    line = FakeReadyLine()
    w._handle_ready([line])
    assert wake.is_set()
    assert w.status()["edges"] == 1
    assert line.consumed == 1, "the event was never read, so it would fire again"


def test_no_ready_lines_does_not_wake_anything() -> None:
    wake = threading.Event()
    w = EdgeWatcher([4], wake)
    w._handle_ready([])
    assert not wake.is_set()


# --- the property that matters ---------------------------------------------


def test_sampling_continues_with_no_interrupts_at_all() -> None:
    """A dead INT line must cost latency, not data.

    No lgpio here, so the watcher never starts and no edge ever arrives. The
    thread has to keep draining on its timer exactly as it did before.
    """
    fake = FakeBus(addresses=(ADDR_PRIMARY,))
    hub = SensorHub(rear_address=ADDR_PRIMARY, watermark=16,
                    int_front_gpio=4, int_rear_gpio=17)
    hub.start(bus=fake)
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not hub._pods[0].stats.present:
            time.sleep(0.02)
        assert hub._pods[0].stats.present, "pod never attached"
        for _ in range(64):
            fake.push(ADDR_PRIMARY, 0.0, 0.0, 1.0)
        before = hub._pods[0].stats.samples
        time.sleep(0.25)
        assert hub._pods[0].stats.samples > before, "sampling stalled without interrupts"
    finally:
        hub.stop()


def test_stop_does_not_wait_out_the_timeout() -> None:
    hub = SensorHub()
    hub.start(bus=FakeBus(addresses=(ADDR_PRIMARY,)))
    t0 = time.monotonic()
    hub.stop()
    assert time.monotonic() - t0 < 1.0, "stop() blocked on the poll timeout"


def test_interrupt_state_is_visible_in_status() -> None:
    hub = SensorHub(int_front_gpio=4, int_rear_gpio=17)
    hub.start(bus=FakeBus(addresses=(ADDR_PRIMARY,)))
    try:
        s = hub.status()["interrupts"]
        assert s is not None
        assert s["gpios"] == [4, 17]
        assert "available" in s and "edges" in s
    finally:
        hub.stop()
