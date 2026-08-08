"""Front/rear isolation measurement.

The number this produces decides whether a rig is really two channels or an
expensive mono, so the things worth protecting are that it refuses to report a
ratio it cannot support, and that it subtracts ambient rather than counting it
as coupling.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time

import pytest

from shaker.audio.bus import AudioBus
from shaker.config import AudioConfig
from shaker.sensors.adxl345 import ADDR_ALT, ADDR_PRIMARY
from shaker.sensors.crosstalk import (
    classify,
    measure,
    ratio_db,
    subtract_baseline,
)
from shaker.sensors.pods import SensorHub

from test_sensors import FakeBus

# Short enough that the whole sequence runs in well under a second.
_FAST = dict(pulse_s=0.30, gap_s=0.12, settle_s=0.06, window_s=0.18, baseline_s=0.10)


# --- the maths -------------------------------------------------------------


def test_ratio_is_negative_dB_and_floored() -> None:
    assert ratio_db(0.05, 0.5) == pytest.approx(-20.0, abs=0.1)
    assert ratio_db(0.5, 0.5) == pytest.approx(0.0, abs=0.1)
    # Silence at the far pod must not produce -inf and reach the UI as JSON.
    assert math.isfinite(ratio_db(0.0, 0.5))
    assert ratio_db(0.1, 0.0) == 0.0


def test_baseline_subtracts_in_power_not_amplitude() -> None:
    """Noise and signal add as energy. Subtracting amplitudes over-corrects."""
    assert subtract_baseline(0.5, 0.3) == pytest.approx(0.4, abs=1e-6)
    # Never negative, however noisy the room was.
    assert subtract_baseline(0.1, 0.4) == 0.0


def test_bands_follow_the_two_channel_argument() -> None:
    assert classify(-20.0)[0] == "good"
    assert classify(-12.0)[0] == "good"
    assert classify(-9.0)[0] == "usable"
    assert classify(-6.0)[0] == "usable"
    assert classify(-3.0)[0] == "poor"
    assert classify(0.0)[0] == "poor"


# --- the sequence ----------------------------------------------------------


class Rig:
    """A simulated rig whose front/rear coupling is a known constant.

    Feeds the FakeBus on a thread, watching the AudioBus's wiring-check state so
    the pods only see motion while the corresponding pulse is playing — the same
    correlation the real measurement depends on.
    """

    def __init__(self, bus: AudioBus, fake: FakeBus, coupling: float,
                 drive_g: float = 0.4, noise_g: float = 0.0) -> None:
        self.bus = bus
        self.fake = fake
        self.coupling = coupling
        self.drive_g = drive_g
        self.noise_g = noise_g
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._seen = 0
        self._started: float | None = None

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        self._t.join(timeout=1.0)

    def _levels(self) -> tuple[float, float]:
        """(front pod, rear pod) amplitudes for wherever the pulse has got to."""
        if self._started is None:
            return (self.noise_g, self.noise_g)
        t = time.monotonic() - self._started
        pulse, gap = self.bus.wiring_pulse_s, self.bus.wiring_gap_s
        if t < pulse:
            return (self.drive_g, self.drive_g * self.coupling)
        if t < pulse + gap:
            return (self.noise_g, self.noise_g)
        if t < 2 * pulse + gap:
            return (self.drive_g * self.coupling, self.drive_g)
        self._started = None
        return (self.noise_g, self.noise_g)

    def _run(self) -> None:
        n = 0
        while not self._stop.is_set():
            if self.bus.wiring_check_count != self._seen:
                self._seen = self.bus.wiring_check_count
                self._started = time.monotonic()
            f, r = self._levels()
            for _ in range(16):
                # A sine at each pod, so RMS is amplitude/sqrt(2) and the ratio
                # between pods is exactly the coupling.
                ph = 2 * math.pi * 50.0 * n / 800.0
                self.fake.push(ADDR_PRIMARY, 0.0, 0.0, 1.0 + f * math.sin(ph))
                self.fake.push(ADDR_ALT, 0.0, 0.0, 1.0 + r * math.sin(ph))
                n += 1
            time.sleep(0.02)


def _rig(coupling: float, **kw) -> tuple[AudioBus, SensorHub, Rig]:
    abus = AudioBus(AudioConfig())
    fake = FakeBus(addresses=(ADDR_PRIMARY, ADDR_ALT))
    hub = SensorHub()
    hub.start(bus=fake)
    rig = Rig(abus, fake, coupling, **kw)
    rig.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not all(
        p.stats.present for p in hub._pods
    ):
        time.sleep(0.02)
    # Let the gravity tracker settle so the AC figure is the shake, not the 1 g.
    time.sleep(0.6)
    return (abus, hub, rig)


def _teardown(hub: SensorHub, rig: Rig) -> None:
    rig.stop()
    hub.stop()


def test_measures_a_known_coupling() -> None:
    abus, hub, rig = _rig(coupling=0.25)          # -12 dB by construction
    try:
        r = asyncio.run(measure(abus, hub, **_FAST))
    finally:
        _teardown(hub, rig)

    assert r.ok, r.reason
    assert r.front_to_rear_db == pytest.approx(-12.0, abs=2.0), r.as_dict()
    assert r.rear_to_front_db == pytest.approx(-12.0, abs=2.0), r.as_dict()


def test_a_well_isolated_rig_reads_good() -> None:
    abus, hub, rig = _rig(coupling=0.05)          # -26 dB
    try:
        r = asyncio.run(measure(abus, hub, **_FAST))
    finally:
        _teardown(hub, rig)
    assert r.ok, r.reason
    assert r.verdict == "good", r.as_dict()


def test_a_rig_that_is_really_mono_reads_poor() -> None:
    abus, hub, rig = _rig(coupling=0.9)
    try:
        r = asyncio.run(measure(abus, hub, **_FAST))
    finally:
        _teardown(hub, rig)
    assert r.ok, r.reason
    assert r.verdict == "poor", r.as_dict()
    assert "mono" in r.detail


def test_a_dead_channel_is_refused_rather_than_reported() -> None:
    """A silent driven pod means an amp or mount fault.

    Dividing one noise floor by another would produce a confident-looking
    figure near 0 dB, which reads as catastrophic crosstalk and would send
    someone off isolating a rig that has a disconnected speaker lead.
    """
    abus, hub, rig = _rig(coupling=0.25, drive_g=0.0, noise_g=0.02)
    try:
        r = asyncio.run(measure(abus, hub, **_FAST))
    finally:
        _teardown(hub, rig)

    assert not r.ok
    assert "barely moved" in (r.reason or "")
    assert r.front_to_rear_db == 0.0


def test_ambient_noise_is_removed_rather_than_counted_as_coupling() -> None:
    """A rig humming at rest must not report that hum as bleed."""
    abus, hub, rig = _rig(coupling=0.05, drive_g=0.5, noise_g=0.05)
    try:
        r = asyncio.run(measure(abus, hub, **_FAST))
    finally:
        _teardown(hub, rig)

    assert r.ok, r.reason
    assert r.baseline_rear_g > 0.0, "the test rig should have a noise floor"
    # Without subtraction the floor alone would put this near -20 dB.
    assert r.front_to_rear_db < -20.0, r.as_dict()


def test_missing_pod_is_refused_with_a_reason() -> None:
    fake = FakeBus(addresses=(ADDR_PRIMARY,))     # rear absent
    hub = SensorHub()
    hub.start(bus=fake)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not hub._pods[0].stats.present:
        time.sleep(0.02)
    try:
        r = asyncio.run(measure(AudioBus(AudioConfig()), hub, **_FAST))
    finally:
        hub.stop()
    assert not r.ok
    assert "rear" in (r.reason or "")


def test_result_is_json_safe() -> None:
    """Every field reaches the browser, so no infinities or NaNs."""
    import json

    abus, hub, rig = _rig(coupling=0.0, drive_g=0.4)
    try:
        r = asyncio.run(measure(abus, hub, **_FAST))
    finally:
        _teardown(hub, rig)
    text = json.dumps(r.as_dict())
    assert "Infinity" not in text and "NaN" not in text
