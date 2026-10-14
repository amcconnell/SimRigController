"""Wake the sampling thread on a pod's FIFO watermark interrupt.

The breakout board routes each pod's INT1 back to its own GPIO — front on 4,
rear on 17 — so the parts can say "I have data" instead of being asked every
20 ms.

What this does not do is reduce work. The sampling cost is the per-sample
Python loop, not the wake-ups, and the FIFO already decouples read timing from
sample timing: the part buffers at its own oscillator rate and timestamps
nothing, so when the Pi drains has no bearing on when the samples were taken.
Interrupts buy latency and nothing else, and latency was never the problem —
the measurement windows are 0.8 seconds long.

So the poll stays, as a floor rather than as the mechanism. An edge wakes the
thread early; if no edge ever arrives — a broken trace, a pod configured
without the watermark, a Pi with no GPIO at all — the timeout fires and
everything behaves exactly as it did before. A missed interrupt must not be
able to stall sampling, because the alternative is a rig that silently stops
measuring and still reports both pods present.

GPIO goes through the character device in pure Python, for the same reason the
status LED does: no compiler on the appliance. See led.py for why.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from shaker.led import CHIP_PATHS

log = logging.getLogger(__name__)

# How long a poll blocks before looping. Only bounds how quickly stop() is
# noticed — an edge wakes the poll immediately regardless.
_POLL_TIMEOUT_S = 0.5


class EdgeWatcher:
    """Sets an Event on a rising edge from any of the given pins.

    Never raises. Absence of GPIO is a state, as it is everywhere else here:
    the watcher reports itself unavailable and the caller's timeout keeps
    doing the work.
    """

    def __init__(self, gpios: list[int], wake: threading.Event) -> None:
        self._gpios = [g for g in gpios if g > 0]
        self._wake = wake
        self._lines: list[Any] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None
        self.edges = 0

    def start(self) -> None:
        if not self._gpios:
            self.error = "no interrupt pins configured"
            return
        try:
            from periphery import GPIO
        except ImportError as exc:  # pragma: no cover - the package installs anywhere
            self.error = f"periphery not available: {exc}"
            return

        last: Exception | None = None
        for path in CHIP_PATHS:
            lines = []
            try:
                for g in self._gpios:
                    lines.append(GPIO(path, g, "in", edge="rising"))
            except Exception as exc:  # noqa: BLE001 - try the next chip
                last = exc
                for line in lines:
                    try:
                        line.close()
                    except Exception:  # noqa: BLE001
                        pass
                continue
            self._lines = lines
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="pod-interrupts", daemon=True
            )
            self._thread.start()
            log.info("pod interrupts on %s, pins %s", path, self._gpios)
            return
        self.error = f"could not claim {self._gpios} on any of {CHIP_PATHS}: {last}"
        log.info("pod interrupts unavailable: %s", self.error)

    def _run(self) -> None:
        from periphery import GPIO

        while not self._stop.is_set():
            try:
                ready = GPIO.poll_multiple(self._lines, _POLL_TIMEOUT_S)[0]
            except Exception as exc:  # noqa: BLE001 - a dead pin must not kill sampling
                self.error = str(exc)
                log.warning("pod interrupt poll failed, falling back to the timer: %s", exc)
                return
            self._handle_ready(ready)

    def _handle_ready(self, ready: list[Any]) -> None:
        """Consume the events and wake the sampler. Split out to be testable
        without a GPIO: the poll above needs a real character device, this does
        not, and this is the half with the behaviour worth pinning."""
        for line in ready:
            try:
                line.read_event()   # consume it, or the pin stays readable forever
            except Exception:  # noqa: BLE001
                pass
            self.edges += 1
        if ready:
            self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_POLL_TIMEOUT_S * 3)
            self._thread = None
        for line in self._lines:
            try:
                line.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        self._lines = []

    def status(self) -> dict[str, Any]:
        return {
            "gpios": self._gpios,
            "available": bool(self._lines),
            "error": self.error,
            "edges": self.edges,
        }
