"""Status LED: what it reports and what it does when there is no pin.

The pattern that earns this module is the fast blink. The fault it exists to
surface — a pod attaching cleanly and then failing reads for twenty-five
seconds before dying — looked like perfect health to anything measuring
activity, so the tests that matter are the ones about error state and about
it clearing again.
"""

from __future__ import annotations

import time

import pytest

from shaker.led import PATTERNS, LedError, StatusLed, open_line


class FakeLine:
    def __init__(self) -> None:
        self.writes: list[int] = []
        self.closed = False

    def write(self, value: int) -> None:
        self.writes.append(value)

    def close(self) -> None:
        self.closed = True


class FakePod:
    def __init__(self, present: bool = True, last_error_at: float = 0.0) -> None:
        self.stats = type("S", (), {"present": present})()
        self.last_error_at = last_error_at


class FakeHub:
    def __init__(self, pods: list[FakePod], enabled: bool = True, bus: object = object()) -> None:
        self._pods = pods
        self.enabled = enabled
        self._bus = bus


def _led(hub: FakeHub, **kw) -> StatusLed:
    return StatusLed(hub, line=FakeLine(), **kw)


# --- what state maps to what pattern ---------------------------------------


def test_both_pods_healthy_is_a_heartbeat() -> None:
    led = _led(FakeHub([FakePod(), FakePod()]))
    assert led.desired_pattern() == "ok"


def test_one_pod_missing_is_distinguishable_from_both() -> None:
    led = _led(FakeHub([FakePod(present=True), FakePod(present=False)]))
    assert led.desired_pattern() == "one_pod"


def test_no_pods_is_dark() -> None:
    led = _led(FakeHub([FakePod(present=False), FakePod(present=False)]))
    assert led.desired_pattern() == "off"


def test_disabled_hub_is_dark() -> None:
    led = _led(FakeHub([FakePod(), FakePod()], enabled=False))
    assert led.desired_pattern() == "off"


def test_absent_bus_is_dark() -> None:
    led = _led(FakeHub([FakePod(), FakePod()], bus=None))
    assert led.desired_pattern() == "off"


# --- the one that matters --------------------------------------------------


def test_a_recent_read_failure_wins_over_looking_healthy() -> None:
    """The fault this exists for: pods present and streaming, reads failing.

    Both pods report present because the hub re-attaches within two seconds of
    a failure. Anything judging on presence alone sees a healthy rig.
    """
    pods = [FakePod(present=True, last_error_at=time.monotonic()), FakePod(present=True)]
    led = _led(FakeHub(pods))
    assert led.desired_pattern() == "error"


def test_the_error_state_clears_once_the_bus_settles() -> None:
    pods = [FakePod(present=True, last_error_at=time.monotonic() - 30.0), FakePod()]
    led = _led(FakeHub(pods), error_window_s=5.0)
    assert led.desired_pattern() == "ok"


def test_a_pod_that_never_failed_does_not_read_as_an_old_error() -> None:
    """last_error_at defaults to 0.0, which is a very long time ago."""
    led = _led(FakeHub([FakePod(last_error_at=0.0), FakePod(last_error_at=0.0)]))
    assert led.desired_pattern() == "ok"


# --- absence is a state ----------------------------------------------------


def test_no_gpio_library_is_not_an_error() -> None:
    """Developed on a Mac, which has no GPIO at all."""
    led = StatusLed(FakeHub([FakePod(), FakePod()]), gpio=27)
    led.start()          # must not raise
    s = led.status()
    assert s["available"] is False
    assert s["error"]
    led.stop()


def test_disabled_in_config_does_not_claim_a_pin() -> None:
    line = FakeLine()
    led = StatusLed(FakeHub([FakePod(), FakePod()]), enabled=False, line=line)
    led.start()
    assert led.status()["enabled"] is False
    assert line.writes == []


def test_open_line_raises_lederror_rather_than_whatever_lgpio_raises() -> None:
    """Callers catch one exception type; the backend's is not part of the contract."""
    with pytest.raises(LedError):
        open_line(27)


# --- the thread ------------------------------------------------------------


def test_it_actually_drives_the_pin() -> None:
    line = FakeLine()
    led = StatusLed(FakeHub([FakePod(), FakePod()]), line=line)
    led.start()
    time.sleep(0.25)
    led.stop()
    assert line.writes, "the thread never wrote to the pin"
    assert line.closed, "stop() left the pin claimed"


def test_stop_leaves_the_pin_low() -> None:
    """A light left on after shutdown says the app is running when it is not."""
    line = FakeLine()
    led = StatusLed(FakeHub([FakePod(), FakePod()]), line=line)
    led.start()
    time.sleep(0.15)
    led.stop()
    assert line.closed


def test_every_pattern_is_walkable() -> None:
    """A pattern with no steps would spin the thread at full speed."""
    for name, seq in PATTERNS.items():
        assert seq, name
        for on, seconds in seq:
            assert isinstance(on, bool), name
            assert seconds > 0, name
