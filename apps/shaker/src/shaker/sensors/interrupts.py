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
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# Same chips as the status LED: a Pi 4 puts the header on 0, a Pi 5 on 4.
_CHIPS = (0, 4)


class EdgeWatcher:
    """Sets an Event on a rising edge from any of the given pins.

    Never raises. Absence of GPIO is a state, as it is everywhere else here:
    the watcher reports itself unavailable and the caller's timeout keeps
    doing the work.
    """

    def __init__(self, gpios: list[int], wake: threading.Event) -> None:
        self._gpios = [g for g in gpios if g > 0]
        self._wake = wake
        self._handle: Any = None
        self._callbacks: list[Any] = []
        self._lgpio: Any = None
        self.error: str | None = None
        self.edges = 0

    def start(self) -> None:
        if not self._gpios:
            self.error = "no interrupt pins configured"
            return
        try:
            import lgpio
        except ImportError as exc:
            self.error = f"lgpio not available: {exc}"
            log.info("pod interrupts unavailable: %s", self.error)
            return

        self._lgpio = lgpio
        last: Exception | None = None
        for chip in _CHIPS:
            try:
                handle = lgpio.gpiochip_open(chip)
            except Exception as exc:  # noqa: BLE001 - try the next chip
                last = exc
                continue
            try:
                for g in self._gpios:
                    lgpio.gpio_claim_alert(handle, g, lgpio.RISING_EDGE)
                    self._callbacks.append(
                        lgpio.callback(handle, g, lgpio.RISING_EDGE, self._on_edge)
                    )
                self._handle = handle
                log.info("pod interrupts on gpiochip%d, pins %s", chip, self._gpios)
                return
            except Exception as exc:  # noqa: BLE001 - a claimed pin is not fatal
                last = exc
                self._release(handle)
        self.error = f"could not claim {self._gpios} on any of {_CHIPS}: {last}"
        log.info("pod interrupts unavailable: %s", self.error)

    def _on_edge(self, chip: int, gpio: int, level: int, timestamp: int) -> None:
        # Runs on lgpio's thread. Deliberately trivial: setting an Event is the
        # whole job, and anything heavier here would be doing sensor work on a
        # callback thread nobody owns.
        self.edges += 1
        self._wake.set()

    def _release(self, handle: Any) -> None:
        for cb in self._callbacks:
            try:
                cb.cancel()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        self._callbacks = []
        try:
            self._lgpio.gpiochip_close(handle)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if self._handle is not None:
            self._release(self._handle)
            self._handle = None

    def status(self) -> dict[str, Any]:
        return {
            "gpios": self._gpios,
            "available": self._handle is not None,
            "error": self.error,
            "edges": self.edges,
        }
