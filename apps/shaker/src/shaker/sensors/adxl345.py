"""ADXL345 three-axis accelerometer over I2C.

Register-level and deliberately dependency-light: the bus is an injected object
with three methods, so the whole driver is testable without hardware and the
app runs unchanged on a Mac, where there is no I2C at all.

Configuration choices worth stating, because two of them look wrong until you
read the datasheet:

**FULL_RES with the +/-16 g range.** In full-resolution mode the scale factor is
fixed at ~3.9 mg/LSB regardless of range, and the word width grows instead —
13 bits at +/-16 g. So the widest range costs nothing in resolution and buys
headroom that a shaker rig genuinely uses: a kerb strike through a rigid mount
is a large transient sitting on top of 1 g of gravity, and a clipped
accelerometer reading is indistinguishable from a real plateau.

**800 Hz ceiling.** The part goes to 3200 Hz, but at 400 kHz I2C the datasheet
caps usable output at 800 Hz — above that the bus cannot drain the FIFO and
samples are silently lost, which would corrupt exactly the transient
measurements this exists for. 800 Hz is four times Nyquist for the 20-120 Hz
band a shaker works in.

**Stream-mode FIFO.** Reading one sample per poll would alias badly at any
poll rate asyncio can offer. The 32-deep FIFO lets a ~20 ms poll collect a
continuous 800 Hz record instead of 50 scattered points.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

# The two addresses the part supports — ALT ADDRESS pin low or high. There is
# no third option, so two pods is the hardware ceiling on one bus.
ADDR_PRIMARY = 0x53    # ALT tied low (module default)
ADDR_ALT = 0x1D        # ALT tied high

_REG_DEVID = 0x00
_REG_OFSX = 0x1E
_REG_BW_RATE = 0x2C
_REG_POWER_CTL = 0x2D
_REG_INT_ENABLE = 0x2E
_REG_DATA_FORMAT = 0x31
_REG_DATAX0 = 0x32
_REG_FIFO_CTL = 0x38
_REG_FIFO_STATUS = 0x39

_DEVID = 0xE5          # fixed; the only way to tell an ADXL345 from a stuck bus

_POWER_MEASURE = 0x08
_FORMAT_FULL_RES = 0x08
_FIFO_STREAM = 0x80    # mode bits 7:6 = 0b10
_FIFO_SAMPLES = 31     # watermark only; stream mode keeps all 32 entries

# Full-resolution scale factor, constant across ranges by design.
_G_PER_LSB = 1.0 / 256.0

# Offset registers are 15.6 mg per LSB — not the same scale as the data.
_OFFSET_G_PER_LSB = 0.0156

_RANGE_BITS = {2: 0b00, 4: 0b01, 8: 0b10, 16: 0b11}

_RATE_CODES = {
    12.5: 0x07, 25: 0x08, 50: 0x09, 100: 0x0A,
    200: 0x0B, 400: 0x0C, 800: 0x0D,
}
MAX_RATE_HZ = 800.0


class I2CBus(Protocol):
    """The subset of smbus2.SMBus this driver needs."""

    def read_byte_data(self, addr: int, reg: int) -> int: ...
    def write_byte_data(self, addr: int, reg: int, value: int) -> None: ...
    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]: ...


class SensorError(RuntimeError):
    """Any failure to talk to the part. Callers degrade rather than crash."""


@dataclass(frozen=True)
class Sample:
    x: float
    y: float
    z: float


def _s16(lo: int, hi: int) -> int:
    v = (hi << 8) | lo
    return v - 65536 if v & 0x8000 else v


def nearest_rate(hz: float) -> float:
    """Snap a requested rate to one the part can actually produce.

    Silently rounding matters more than it looks: an unsupported code would
    leave BW_RATE at whatever it was, and every later measurement would be
    computed against a sample rate the device is not using.
    """
    capped = min(float(hz), MAX_RATE_HZ)
    return min(_RATE_CODES, key=lambda r: abs(r - capped))


class ADXL345:
    """One accelerometer. Construct, then `configure()`."""

    def __init__(self, bus: I2CBus, address: int = ADDR_PRIMARY) -> None:
        self.bus = bus
        self.address = address
        self.rate_hz: float = 0.0
        self.range_g: int = 16

    def probe(self) -> bool:
        """True if an ADXL345 answers at this address.

        Checks DEVID rather than merely completing a read: a floating or
        shorted bus can ACK and return 0x00 or 0xFF, which would otherwise
        present as a working sensor reading a constant zero.
        """
        try:
            return self.bus.read_byte_data(self.address, _REG_DEVID) == _DEVID
        except OSError:
            return False

    def configure(self, rate_hz: float = 800.0, range_g: int = 16) -> None:
        if range_g not in _RANGE_BITS:
            raise ValueError(f"range must be one of {sorted(_RANGE_BITS)}, got {range_g}")
        rate = nearest_rate(rate_hz)
        try:
            # Standby while reconfiguring — the datasheet only guarantees the
            # format and FIFO registers latch cleanly when not measuring.
            self.bus.write_byte_data(self.address, _REG_POWER_CTL, 0x00)
            self.bus.write_byte_data(self.address, _REG_BW_RATE, _RATE_CODES[rate])
            self.bus.write_byte_data(
                self.address, _REG_DATA_FORMAT, _FORMAT_FULL_RES | _RANGE_BITS[range_g]
            )
            self.bus.write_byte_data(self.address, _REG_INT_ENABLE, 0x00)
            # Bypass first: switching straight into stream leaves whatever was
            # already buffered, so the first poll would return stale samples
            # timestamped as current.
            self.bus.write_byte_data(self.address, _REG_FIFO_CTL, 0x00)
            self.bus.write_byte_data(self.address, _REG_FIFO_CTL, _FIFO_STREAM | _FIFO_SAMPLES)
            self.bus.write_byte_data(self.address, _REG_POWER_CTL, _POWER_MEASURE)
        except OSError as exc:
            raise SensorError(f"configuring 0x{self.address:02x}: {exc}") from exc
        self.rate_hz = rate
        self.range_g = range_g

    def set_offsets(self, x_g: float = 0.0, y_g: float = 0.0, z_g: float = 0.0) -> None:
        """Write the trim registers, in g. Applied by the part before the FIFO."""
        vals = []
        for g in (x_g, y_g, z_g):
            # Registers are signed 8-bit at 15.6 mg/LSB, so the usable trim is
            # about +/-2 g. Clamped rather than wrapped: a silently wrapped
            # offset would invert the correction.
            counts = max(-128, min(127, int(round(-g / _OFFSET_G_PER_LSB))))
            vals.append(counts & 0xFF)
        try:
            for i, v in enumerate(vals):
                self.bus.write_byte_data(self.address, _REG_OFSX + i, v)
        except OSError as exc:
            raise SensorError(f"writing offsets to 0x{self.address:02x}: {exc}") from exc

    def read_one(self) -> Sample:
        """A single current reading, bypassing the FIFO. For probing only."""
        try:
            d = self.bus.read_i2c_block_data(self.address, _REG_DATAX0, 6)
        except OSError as exc:
            raise SensorError(f"reading 0x{self.address:02x}: {exc}") from exc
        return Sample(
            _s16(d[0], d[1]) * _G_PER_LSB,
            _s16(d[2], d[3]) * _G_PER_LSB,
            _s16(d[4], d[5]) * _G_PER_LSB,
        )

    def read_fifo(self) -> list[Sample]:
        """Drain whatever the FIFO holds, oldest first.

        Returns an empty list when nothing has accumulated, which is normal at
        a poll rate faster than the sample rate rather than an error.
        """
        try:
            entries = self.bus.read_byte_data(self.address, _REG_FIFO_STATUS) & 0x3F
            out = []
            for _ in range(entries):
                d = self.bus.read_i2c_block_data(self.address, _REG_DATAX0, 6)
                out.append(Sample(
                    _s16(d[0], d[1]) * _G_PER_LSB,
                    _s16(d[2], d[3]) * _G_PER_LSB,
                    _s16(d[4], d[5]) * _G_PER_LSB,
                ))
            return out
        except OSError as exc:
            raise SensorError(f"reading FIFO on 0x{self.address:02x}: {exc}") from exc


def open_bus(number: int) -> I2CBus:
    """Open a hardware I2C bus, or explain why not.

    smbus2 imports fine on a Mac and fails at open, so both the missing-package
    and no-such-bus cases arrive here as the same kind of problem and get the
    same treatment: raise SensorError, let the caller carry on without sensors.
    """
    try:
        from smbus2 import SMBus
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SensorError(f"smbus2 not installed: {exc}") from exc
    try:
        return SMBus(number)
    except (OSError, FileNotFoundError) as exc:
        raise SensorError(
            f"cannot open /dev/i2c-{number}: {exc}. "
            "On a Pi, enable I2C (dtparam=i2c_arm=on) and check group membership."
        ) from exc
