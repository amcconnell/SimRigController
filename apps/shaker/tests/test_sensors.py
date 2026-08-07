"""ADXL345 driver and the pod manager, against a simulated bus.

No hardware, deliberately. The pods do not exist yet and the app has to behave
on a Mac with no I2C at all, so the absence paths matter at least as much as
the working one — a sensor fault must degrade to "not present" rather than take
the rig down.
"""

from __future__ import annotations

import math

import pytest

from shaker.sensors.adxl345 import (
    ADDR_ALT,
    ADDR_PRIMARY,
    ADXL345,
    MAX_RATE_HZ,
    SensorError,
    nearest_rate,
)
from shaker.sensors.pods import Pod, SensorHub

_DEVID_REG = 0x00
_BW_RATE = 0x2C
_POWER_CTL = 0x2D
_DATA_FORMAT = 0x31
_DATAX0 = 0x32
_FIFO_CTL = 0x38
_FIFO_STATUS = 0x39
_OFSX = 0x1E


class FakeBus:
    """An ADXL345 that lives in a dict.

    `queue` is what the FIFO will hand out, in raw counts. `fail_after` makes
    the part start raising OSError, which is what a yanked Cat5 drop looks like
    from Python.
    """

    def __init__(self, addresses=(ADDR_PRIMARY,), devid: int = 0xE5) -> None:
        self.regs: dict[int, dict[int, int]] = {
            a: {_DEVID_REG: devid} for a in addresses
        }
        self.queue: dict[int, list[tuple[int, int, int]]] = {a: [] for a in addresses}
        self.writes: list[tuple[int, int, int]] = []
        self.fail_after: int | None = None
        self._ops = 0

    def _check(self, addr: int) -> None:
        self._ops += 1
        if self.fail_after is not None and self._ops > self.fail_after:
            raise OSError(5, "Input/output error")
        if addr not in self.regs:
            raise OSError(121, "Remote I/O error")

    def read_byte_data(self, addr: int, reg: int) -> int:
        self._check(addr)
        if reg == _FIFO_STATUS:
            return min(len(self.queue[addr]), 32)
        return self.regs[addr].get(reg, 0)

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        self._check(addr)
        self.regs[addr][reg] = value
        self.writes.append((addr, reg, value))

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        self._check(addr)
        if reg != _DATAX0:
            return [0] * length
        if not self.queue[addr]:
            return [0] * length
        x, y, z = self.queue[addr].pop(0)
        out = []
        for v in (x, y, z):
            u = v & 0xFFFF
            out += [u & 0xFF, (u >> 8) & 0xFF]
        return out

    def push(self, addr: int, x_g: float, y_g: float, z_g: float, n: int = 1) -> None:
        """Queue n samples expressed in g, at the full-resolution scale."""
        counts = tuple(int(round(v * 256.0)) for v in (x_g, y_g, z_g))
        self.queue[addr].extend([counts] * n)


# --- driver ----------------------------------------------------------------


def test_probe_accepts_only_the_real_part_id() -> None:
    """A floating bus can ACK and return 0x00 or 0xFF.

    Probing on "the read succeeded" would present a dead bus as a sensor
    reading a constant zero, which is the worst possible failure here: every
    downstream measurement would look plausible and be meaningless.
    """
    assert ADXL345(FakeBus(), ADDR_PRIMARY).probe()
    assert not ADXL345(FakeBus(devid=0x00), ADDR_PRIMARY).probe()
    assert not ADXL345(FakeBus(devid=0xFF), ADDR_PRIMARY).probe()


def test_probe_on_an_empty_address_is_false_not_an_exception() -> None:
    assert not ADXL345(FakeBus(addresses=(ADDR_PRIMARY,)), ADDR_ALT).probe()


def test_configure_sets_full_resolution_and_measure_mode() -> None:
    bus = FakeBus()
    ADXL345(bus, ADDR_PRIMARY).configure(rate_hz=800, range_g=16)
    regs = bus.regs[ADDR_PRIMARY]
    assert regs[_DATA_FORMAT] & 0x08, "FULL_RES must be set"
    assert regs[_DATA_FORMAT] & 0x03 == 0b11, "+/-16 g"
    assert regs[_POWER_CTL] == 0x08, "measure mode"
    assert regs[_BW_RATE] == 0x0D, "800 Hz"
    assert regs[_FIFO_CTL] & 0xC0 == 0x80, "stream mode"


def test_configure_clears_the_fifo_before_arming_it() -> None:
    """Otherwise the first poll returns samples from before configuration."""
    bus = FakeBus()
    ADXL345(bus, ADDR_PRIMARY).configure()
    fifo_writes = [v for a, r, v in bus.writes if r == _FIFO_CTL]
    assert fifo_writes[0] == 0x00, fifo_writes
    assert fifo_writes[-1] & 0xC0 == 0x80


def test_configure_standby_then_measure() -> None:
    bus = FakeBus()
    ADXL345(bus, ADDR_PRIMARY).configure()
    power = [v for a, r, v in bus.writes if r == _POWER_CTL]
    assert power == [0x00, 0x08], power


def test_configure_rejects_a_bad_range() -> None:
    with pytest.raises(ValueError):
        ADXL345(FakeBus(), ADDR_PRIMARY).configure(range_g=6)


def test_rate_is_snapped_and_capped() -> None:
    """An unsupported code would leave BW_RATE untouched, so every later
    calculation would use a sample rate the part is not running at."""
    assert nearest_rate(800) == 800
    assert nearest_rate(3200) == MAX_RATE_HZ, "I2C cannot drain faster than 800 Hz"
    assert nearest_rate(90) == 100
    assert nearest_rate(0.1) == 12.5

    bus = FakeBus()
    dev = ADXL345(bus, ADDR_PRIMARY)
    dev.configure(rate_hz=3200)
    assert dev.rate_hz == 800.0
    assert bus.regs[ADDR_PRIMARY][_BW_RATE] == 0x0D


def test_read_decodes_signed_axes_in_g() -> None:
    bus = FakeBus()
    bus.push(ADDR_PRIMARY, 0.5, -0.25, 1.0)
    s = ADXL345(bus, ADDR_PRIMARY).read_one()
    assert s.x == pytest.approx(0.5, abs=0.005)
    assert s.y == pytest.approx(-0.25, abs=0.005)
    assert s.z == pytest.approx(1.0, abs=0.005)


def test_read_fifo_drains_in_order_and_empties() -> None:
    bus = FakeBus()
    dev = ADXL345(bus, ADDR_PRIMARY)
    for i in range(5):
        bus.push(ADDR_PRIMARY, i * 0.1, 0.0, 1.0)
    got = dev.read_fifo()
    assert len(got) == 5
    assert [round(s.x, 2) for s in got] == [0.0, 0.1, 0.2, 0.3, 0.4]
    assert dev.read_fifo() == [], "a drained FIFO is empty, not an error"


def test_bus_failure_raises_sensor_error_not_oserror() -> None:
    bus = FakeBus()
    dev = ADXL345(bus, ADDR_PRIMARY)
    dev.configure()
    bus.fail_after = 0
    with pytest.raises(SensorError):
        dev.read_fifo()


def test_offsets_are_written_with_the_right_sign_and_clamped() -> None:
    """The trim registers cancel a reading, so the sign is inverted; and they
    are 8-bit, so an out-of-range request must clamp rather than wrap."""
    bus = FakeBus()
    dev = ADXL345(bus, ADDR_PRIMARY)
    dev.set_offsets(x_g=0.0156, y_g=-0.0156, z_g=0.0)
    assert bus.regs[ADDR_PRIMARY][_OFSX] == 0xFF          # -1 count
    assert bus.regs[ADDR_PRIMARY][_OFSX + 1] == 0x01
    dev.set_offsets(x_g=100.0)
    assert bus.regs[ADDR_PRIMARY][_OFSX] == 0x80          # -128, clamped


# --- pods ------------------------------------------------------------------


def _spin(pod: Pod, bus: FakeBus, polls: int = 200, dt: float = 0.02) -> None:
    for _ in range(polls):
        pod.poll(dt)


def test_pod_tracks_gravity_and_reports_orientation() -> None:
    bus = FakeBus()
    pod = Pod("front", ADDR_PRIMARY, 800.0, 16)
    pod.attach(bus)
    assert pod.stats.present

    # 200 polls x 32 samples at 800 Hz is 8 s — five time constants of the
    # gravity tracker, so it has genuinely settled rather than nearly.
    for _ in range(200):
        bus.push(ADDR_PRIMARY, 0.0, 0.0, 1.0, n=32)
        pod.poll(0.02)

    assert pod.stats.tilt_g == pytest.approx(1.0, abs=0.05)
    assert pod.stats.orientation() == "Z+"
    # A still pod reads zero vibration, not its own noise floor.
    assert pod.stats.vibration_rms_g == 0.0


def test_pod_reports_a_pod_mounted_on_its_side() -> None:
    bus = FakeBus()
    pod = Pod("rear", ADDR_PRIMARY, 800.0, 16)
    pod.attach(bus)
    for _ in range(200):
        bus.push(ADDR_PRIMARY, 0.0, -1.0, 0.0, n=32)
        pod.poll(0.02)
    assert pod.stats.orientation() == "Y-"


def test_vibration_measures_the_ac_component_only() -> None:
    """Gravity is a constant that says nothing about what the rig is doing.

    A pod shaking at 50 Hz on top of 1 g must report the shake, not the 1 g.
    """
    bus = FakeBus()
    pod = Pod("front", ADDR_PRIMARY, 800.0, 16)
    pod.attach(bus)

    n = 0
    for _ in range(200):
        batch = []
        for _ in range(32):
            batch.append(0.2 * math.sin(2 * math.pi * 50.0 * n / 800.0))
            n += 1
        for v in batch:
            bus.push(ADDR_PRIMARY, 0.0, 0.0, 1.0 + v)
        pod.poll(0.02)

    # RMS of a 0.2 g amplitude sine is 0.2/sqrt(2).
    assert pod.stats.vibration_rms_g == pytest.approx(0.2 / math.sqrt(2), rel=0.15)
    assert pod.stats.tilt_g == pytest.approx(1.0, abs=0.05)


def test_peak_survives_between_ui_polls() -> None:
    """One tap during installation has to be visible half a second later."""
    bus = FakeBus()
    pod = Pod("front", ADDR_PRIMARY, 800.0, 16)
    pod.attach(bus)
    for _ in range(200):
        bus.push(ADDR_PRIMARY, 0.0, 0.0, 1.0, n=32)
        pod.poll(0.02)

    bus.push(ADDR_PRIMARY, 0.0, 0.0, 2.5, n=4)   # a whack
    pod.poll(0.02)
    assert pod.stats.vibration_peak_g > 0.5

    for _ in range(10):                           # ~200 ms later
        pod.poll(0.02)
    assert pod.stats.vibration_peak_g > 0.1


def test_pod_absent_is_a_state_not_an_error() -> None:
    bus = FakeBus(addresses=(ADDR_PRIMARY,))
    pod = Pod("rear", ADDR_ALT, 800.0, 16)
    pod.attach(bus)
    assert not pod.stats.present
    assert pod.stats.error is None
    pod.poll(0.02)  # must not raise
    assert pod.status()["present"] is False


def test_pod_that_fails_mid_session_drops_to_absent() -> None:
    """A yanked Cat5 drop must not take the telemetry loop down."""
    bus = FakeBus()
    pod = Pod("front", ADDR_PRIMARY, 800.0, 16)
    pod.attach(bus)
    assert pod.stats.present

    bus.push(ADDR_PRIMARY, 0.0, 0.0, 1.0, n=8)
    bus.fail_after = 0
    pod.poll(0.02)

    assert not pod.stats.present
    assert pod.stats.error
    pod.poll(0.02)  # still safe once disarmed


def test_pod_reattaches_when_plugged_back_in() -> None:
    """Installation is done with the app running; hot-plug has to work."""
    bus = FakeBus(addresses=())
    pod = Pod("front", ADDR_PRIMARY, 800.0, 16)
    pod.attach(bus)
    assert not pod.stats.present

    bus.regs[ADDR_PRIMARY] = {_DEVID_REG: 0xE5}
    bus.queue[ADDR_PRIMARY] = []
    pod._last_probe = 0.0        # skip the re-probe backoff
    pod.attach(bus)
    assert pod.stats.present


def test_hub_without_a_bus_reports_rather_than_raises() -> None:
    """The development Mac, and a Pi before anything is wired."""
    hub = SensorHub(bus_number=99)
    hub.start()
    st = hub.status()
    assert st["available"] is False
    assert st["error"]
    assert st["any_present"] is False
    assert len(st["pods"]) == 2
    hub.stop()


def test_hub_disabled_does_not_touch_the_bus() -> None:
    hub = SensorHub(enabled=False)
    hub.start()
    assert hub.status()["available"] is False
    assert hub.status()["error"] == "disabled in config"
    hub.stop()


def test_hub_finds_both_pods_and_keeps_them_apart() -> None:
    bus = FakeBus(addresses=(ADDR_PRIMARY, ADDR_ALT))
    hub = SensorHub()
    hub.start(bus=bus)
    try:
        deadline = 2.0
        step = 0.02
        waited = 0.0
        while waited < deadline and not hub.status()["any_present"]:
            import time
            time.sleep(step)
            waited += step
        st = hub.status()
        assert st["available"] is True
        assert [p["name"] for p in st["pods"]] == ["front", "rear"]
        assert st["pods"][0]["address"] == "0x53"
        assert st["pods"][1]["address"] == "0x1d"
        assert all(p["present"] for p in st["pods"]), st
    finally:
        hub.stop()


def test_hub_stop_is_idempotent() -> None:
    hub = SensorHub(enabled=False)
    hub.start()
    hub.stop()
    hub.stop()
