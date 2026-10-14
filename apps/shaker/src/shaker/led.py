"""Status LED on the breakout board's front edge.

Deliberately not an activity light. "There is traffic on the bus" is the least
useful thing this could report, because the failure that made the LED worth
fitting was never an absence of traffic: the front pod attached cleanly, threw
Errno 5 for twenty-five seconds, and died, while the bus stayed busy
throughout. An activity light would have looked healthy the whole time.

So it reports health instead, and the pattern that matters is the fast blink —
reads are failing right now. That is the state that used to be invisible until
a measurement came back wrong hours later.

Everything here degrades to nothing. The GPIO library only exists on Linux, the
line only exists on a Pi, and this is developed on a Mac — so absence is a
state rather than an error, exactly as it is for the pods. A rig with no LED
fitted behaves identically to one that has it, minus the light.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Which gpiochip to claim. A Pi 4 exposes the 40-pin header on chip 0; a Pi 5
# moved it to chip 4. Tried in order rather than configured, because the right
# answer is a property of the board and not something anyone should have to
# know to get a light working.
_CHIPS = (0, 4)

# How recently a pod must have failed a read for the LED to call it an error.
# Long enough that a fault which recurs every few seconds reads as continuous
# rather than flickering between states, short enough that it stops promptly
# once the bus settles.
ERROR_WINDOW_S = 5.0

# (on, seconds) steps, walked in order and repeated.
PATTERNS: dict[str, tuple[tuple[bool, float], ...]] = {
    # Nothing to say: sensors disabled, no bus, or no pod detected.
    "off": ((False, 0.5),),
    # One pod of two. Slow and obviously deliberate, so it reads as a state
    # rather than as something broken.
    "one_pod": ((True, 0.5), (False, 0.5)),
    # Both pods streaming and no recent failures. A brief pulse rather than
    # steady-on: a light that is simply lit tells you the Pi has power, which
    # you already knew. A heartbeat tells you something is still running.
    "ok": ((True, 0.06), (False, 1.94)),
    # Reads are failing. Fast enough to be unmistakable from across a room,
    # because this is the one state worth crossing a room for.
    "error": ((True, 0.08), (False, 0.08)),
}


class LedError(Exception):
    pass


class GpioLine(Protocol):
    """Just enough GPIO to drive one pin, so tests need no hardware."""

    def write(self, value: int) -> None: ...
    def close(self) -> None: ...


class _LgpioLine:
    def __init__(self, gpio: int) -> None:
        import lgpio  # imported here: the package only exists on Linux

        self._lgpio = lgpio
        self._gpio = gpio
        last: Exception | None = None
        for chip in _CHIPS:
            try:
                self._handle = lgpio.gpiochip_open(chip)
                lgpio.gpio_claim_output(self._handle, gpio, 0)
                return
            except Exception as exc:  # noqa: BLE001 - any failure means try the next chip
                last = exc
        raise LedError(f"no gpiochip in {_CHIPS} would give up GPIO{gpio}: {last}")

    def write(self, value: int) -> None:
        self._lgpio.gpio_write(self._handle, self._gpio, value)

    def close(self) -> None:
        try:
            self._lgpio.gpio_write(self._handle, self._gpio, 0)
            self._lgpio.gpiochip_close(self._handle)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


def open_line(gpio: int) -> GpioLine:
    """Claim the pin. Raises LedError anywhere that cannot, which is most places."""
    try:
        return _LgpioLine(gpio)
    except ImportError as exc:
        raise LedError(f"lgpio not available: {exc}") from exc
    except LedError:
        raise
    except Exception as exc:  # noqa: BLE001 - a missing pin must not take the app down
        raise LedError(str(exc)) from exc


class StatusLed:
    """Drives one pin from the sensor hub's health, on its own thread."""

    def __init__(
        self,
        hub: Any,
        gpio: int = 27,
        enabled: bool = True,
        line: GpioLine | None = None,
        error_window_s: float = ERROR_WINDOW_S,
    ) -> None:
        self._hub = hub
        self._gpio = gpio
        self._enabled = enabled
        self._line = line
        self._error_window_s = error_window_s
        self._error: str | None = None
        self._pattern = "off"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Claim the pin and begin blinking. Never raises — absence is a state."""
        if not self._enabled:
            self._error = "disabled in config"
            return
        if self._line is None:
            try:
                self._line = open_line(self._gpio)
            except LedError as exc:
                self._error = str(exc)
                log.info("status LED unavailable: %s", exc)
                return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="status-led", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._line is not None:
            self._line.close()
            self._line = None

    def desired_pattern(self) -> str:
        """Map the hub's state onto a pattern name."""
        hub = self._hub
        pods = list(getattr(hub, "_pods", []))
        if not getattr(hub, "enabled", False) or getattr(hub, "_bus", None) is None:
            return "off"
        if self._recent_error(pods):
            return "error"
        present = sum(1 for p in pods if p.stats.present)
        if present == 0:
            return "off"
        if present < len(pods):
            return "one_pod"
        return "ok"

    def _recent_error(self, pods: list[Any]) -> bool:
        now = time.monotonic()
        for p in pods:
            at = getattr(p, "last_error_at", 0.0)
            if at and now - at < self._error_window_s:
                return True
        return False

    def _run(self) -> None:
        step = 0
        while not self._stop.is_set():
            want = self.desired_pattern()
            if want != self._pattern:
                # Restart the pattern rather than continuing mid-sequence, so a
                # transition into "error" lights immediately instead of waiting
                # out the long dark half of a heartbeat.
                self._pattern = want
                step = 0
            seq = PATTERNS[want]
            on, seconds = seq[step % len(seq)]
            step += 1
            if self._line is not None:
                try:
                    self._line.write(1 if on else 0)
                except Exception as exc:  # noqa: BLE001 - a dead pin must not kill the thread
                    self._error = str(exc)
                    log.warning("status LED write failed: %s", exc)
                    return
            self._stop.wait(seconds)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "gpio": self._gpio,
            "available": self._line is not None,
            "error": self._error,
            "pattern": self._pattern,
        }
