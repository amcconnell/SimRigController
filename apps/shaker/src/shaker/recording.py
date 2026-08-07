"""Full-rate capture of a driving session, for replaying through the DSP later.

Every tuning decision this project has made has been judged against memory of a
previous session — a different track, a different car, and a subjective
impression days old. That is the weakest link in the loop, and it is not one
more sensors fix. A recorded session plus a replay harness turns "feels more
refined" into the same input through two parameter sets, which is the only way
to know whether a change was an improvement or a mood.

So the format is chosen for *replay fidelity*, not for reporting:

- Whole parsed packets, not derived features. Feature extraction is code under
  test too, and a recording that stored `suspension_activity` could never be
  used to check the thing that computed it.
- Every packet, at the rate they arrive. `scripts/motion_probe.py` samples
  `/api/status` over HTTP, which is fine for identifying a field but drops most
  frames — useless for anything transient, which is most of what the rig
  conveys.
- Packets the app would reject (menus, pauses, replays) are kept. The gates in
  `AudioBus.push_packet` are also under test; a recording that pre-applied them
  would hide their bugs.

Two clocks are stored per packet, because they answer different questions.
`packet_id` is GT7's physics frame counter — the right clock for replay, since
it makes a dropped packet a visible gap rather than a phantom time-warp. `t` is
the Pi's monotonic receive time, which is what a later accelerometer capture
would be aligned against to measure end-to-end latency.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Iterator

from shaker.gt7.protocol import TelemetryPacket

log = logging.getLogger(__name__)

RECORDINGS_DIR = Path(__file__).resolve().parent.parent.parent / "recordings"

SCHEMA_VERSION = 1

# Size ceiling, enforced because this writes to the SD card a Pi boots from and
# a session left recording overnight would otherwise fill it. At roughly 60 Hz
# and ~350 bytes a packet this is a bit over three hours — far longer than any
# real session, so hitting it means something went wrong rather than that a
# limit was too mean.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024

# Flush cadence. Buffered writes keep the packet path cheap; flushing about
# once a second bounds what a hard power-off can lose without doing a syscall
# per packet.
_FLUSH_EVERY = 60

# Float precision. Positions are metres, so six decimals is a micrometre —
# comfortably past anything the physics engine means, while cutting the file
# to roughly a third of what full repr() output costs.
_PRECISION = 6

_FLOAT_FIELDS = tuple(
    f.name for f in fields(TelemetryPacket) if f.type in ("float", float)
)


def _row(packet: TelemetryPacket, t: float) -> dict[str, Any]:
    d = asdict(packet)
    for k in _FLOAT_FIELDS:
        d[k] = round(d[k], _PRECISION)
    d["t"] = round(t, 4)
    return d


class SessionRecorder:
    """Append parsed packets to a JSONL file until stopped or capped.

    Lives on the asyncio side and is driven straight from the packet callback,
    so it sees every frame. Writes are buffered and flushed periodically rather
    than per packet — file I/O on the receive path is acceptable at 60 Hz, a
    flush per packet would not be.

    Never raises into the packet path. A recorder that takes the rig down
    because a disk filled is worse than one that quietly stops recording, so a
    write failure logs once and disarms.
    """

    def __init__(self, directory: Path = RECORDINGS_DIR) -> None:
        self._dir = directory
        self._fh: Any = None
        self._path: Path | None = None
        self._packets = 0
        self._bytes = 0
        self._max_bytes = DEFAULT_MAX_BYTES
        self._started_mono = 0.0
        self._error: str | None = None

    @property
    def recording(self) -> bool:
        return self._fh is not None

    def start(self, name: str | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> Path:
        """Begin a new recording. Restarting while active rolls to a new file."""
        if self.recording:
            self.stop()
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"session-{stamp}" if not name else f"session-{stamp}-{_slug(name)}"
        path = self._dir / f"{stem}.jsonl"

        header = {
            "schema": SCHEMA_VERSION,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            # Field order is not load-bearing (rows are objects), but recording
            # it makes a file self-describing if the dataclass later changes.
            "packet_fields": [f.name for f in fields(TelemetryPacket)],
        }
        self._fh = path.open("w", buffering=1 << 16)
        self._fh.write(json.dumps(header) + "\n")
        self._path = path
        self._packets = 0
        self._bytes = 0
        self._max_bytes = max_bytes
        self._started_mono = time.monotonic()
        self._error = None
        log.info("recording session to %s", path)
        return path

    def on_packet(self, packet: TelemetryPacket) -> None:
        """Packet-path hook. Cheap, and swallows its own failures by contract."""
        if self._fh is None:
            return
        try:
            line = json.dumps(_row(packet, time.monotonic() - self._started_mono))
            self._fh.write(line + "\n")
            self._bytes += len(line) + 1
            self._packets += 1
            if self._packets % _FLUSH_EVERY == 0:
                self._fh.flush()
            if self._bytes >= self._max_bytes:
                log.warning("recording hit the %d-byte cap; stopping", self._max_bytes)
                self._error = "size cap reached"
                self._close()
        except (OSError, TypeError, ValueError) as exc:
            log.exception("recording write failed; stopping")
            self._error = str(exc)
            self._close()

    def stop(self) -> dict[str, Any]:
        # Close first. Reading status beforehand returns `recording: true` to a
        # caller whose whole request was to make that false, and the UI drives
        # its button off exactly that field.
        self._close()
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "recording": self.recording,
            "path": str(self._path) if self._path else None,
            "name": self._path.name if self._path else None,
            "packets": self._packets,
            "bytes": self._bytes,
            "seconds": (
                round(time.monotonic() - self._started_mono, 1) if self.recording else
                round(self._elapsed_at_stop, 1)
            ),
            "error": self._error,
        }

    _elapsed_at_stop: float = 0.0

    def _close(self) -> None:
        if self._fh is None:
            return
        self._elapsed_at_stop = time.monotonic() - self._started_mono
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            # Deliberately broad. This is the teardown the write path calls
            # *because* something already went wrong, so it is the one place
            # that must not raise — and it is not only OSError: flushing an
            # already-closed handle raises ValueError, which would propagate
            # straight out of on_packet and take the telemetry loop with it.
            log.exception("failed to close recording cleanly")
        self._fh = None
        log.info("recording stopped: %s packets to %s", self._packets, self._path)


def _slug(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in name.strip()]
    return "".join(keep).strip("-")[:40] or "session"


def read_session(path: Path) -> tuple[dict[str, Any], Iterator[TelemetryPacket]]:
    """Read a recording back as (header, packets).

    The inverse of the writer, and the entry point a replay harness will use.
    Unknown keys are dropped rather than raising so a recording outlives a
    schema change — the same tolerance profiles have, for the same reason.
    """
    known = {f.name for f in fields(TelemetryPacket)}

    with path.open() as fh:
        first = fh.readline()
        header = json.loads(first) if first.strip() else {}
        if "schema" not in header:
            raise ValueError(f"{path} has no header line")

    def rows() -> Iterator[TelemetryPacket]:
        with path.open() as fh:
            fh.readline()  # header
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                yield TelemetryPacket(**{k: v for k, v in d.items() if k in known})

    return header, rows()


def list_sessions(directory: Path = RECORDINGS_DIR) -> list[dict[str, Any]]:
    """Newest first. Cheap enough to serve on every status poll."""
    if not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("session-*.jsonl"), reverse=True):
        try:
            out.append({"name": p.name, "bytes": p.stat().st_size})
        except OSError:
            continue
    return out
