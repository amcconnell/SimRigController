"""Push a recorded session back through the DSP, offline and deterministically.

This is the half of the loop that makes a tuning change checkable. Recording a
lap only helps if the same lap can be rendered twice under different settings,
because otherwise every comparison is against a memory of a different track in a
different car. Nothing here touches an audio device: it drives the real
`AudioOutput._callback` with the real `AudioBus`, so what it measures is the
code that runs on the rig rather than a model of it.

Packets are scheduled by **packet_id**, not by the recorded arrival time.
packet_id is GT7's physics frame counter, so it is immune to network jitter and
to how loaded the Pi happened to be — two replays of one file give bit-identical
output, which is the whole point of an A/B. A dropped packet still shows up
correctly, as a real gap in time rather than as a missing sample. The recorded
`t` remains in the file for aligning other sensors later, where wall-clock is
what matters.

The headline number is **crest factor**, peak over RMS. It is the one that
answers the question the limiter panel raises: a mix driven too hard does not
distort, it flattens, and flattening shows up here as crest collapsing while
RMS climbs. Peak alone cannot see it — the limiter pins peak to the ceiling by
construction, so two very differently-behaved settings report the same peak.
"""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from shaker.audio.bus import AudioBus
from shaker.audio.stream import AudioOutput
from shaker.config import AudioConfig
from shaker.gt7.protocol import TelemetryPacket

# GT7's physics rate, and the same constant the feature extraction assumes.
_TELEMETRY_RATE_HZ = 60.0

# A packet_id step beyond this is a session restart or a counter wrap rather
# than a dropped frame — ten seconds of silence inserted mid-replay would
# rewrite the result. Treated as a single frame instead.
_MAX_SANE_ID_GAP = 600

# Rendered after the last packet so effect releases finish inside the
# measurement. Comfortably longer than the slowest release in the chain.
_TAIL_S = 1.5

# Matches the limiter's own "is it working" threshold in stream.py.
_LIMIT_ACTIVE_GAIN = 0.99


def _db(x: float) -> float:
    return -20.0 * math.log10(max(x, 1e-12))


@dataclass
class ChannelStats:
    peak: float = 0.0
    rms: float = 0.0

    @property
    def crest_db(self) -> float:
        """Peak over RMS, in dB. Falls as the mix is compressed."""
        if self.rms <= 0.0:
            return 0.0
        return 20.0 * math.log10(self.peak / self.rms)


@dataclass
class ReplayResult:
    seconds: float = 0.0
    frames: int = 0
    channels: int = 1
    packets: int = 0
    channel: list[ChannelStats] = field(default_factory=list)
    # Share of samples that reached full scale. The limiter should hold this at
    # zero; anything else means it was outrun inside a block.
    clipped_pct: float = 0.0
    # Session-wide limiter statistics, computed per block rather than read off
    # the live meter, which decays and is built for a 2 Hz UI poll.
    limiter_peak_db: float = 0.0
    limiter_duty_pct: float = 0.0
    audio: np.ndarray | None = None

    def summary(self) -> str:
        lines = [
            f"{self.seconds:.1f}s   {self.packets} packets   "
            f"{self.channels}ch @ {self.frames} frames",
            f"limiter: peak {self.limiter_peak_db:.2f} dB   "
            f"duty {self.limiter_duty_pct:.1f}%   clipped {self.clipped_pct:.4f}%",
        ]
        names = ("front", "rear")
        for i, c in enumerate(self.channel):
            label = names[i] if i < len(names) and self.channels > 1 else f"ch{i}"
            lines.append(
                f"{label:>6}: peak {c.peak:.4f} ({_db(c.peak):5.2f} dBFS)   "
                f"rms {c.rms:.4f}   crest {c.crest_db:5.2f} dB"
            )
        return "\n".join(lines)


def schedule(packets: Sequence[TelemetryPacket]) -> list[float]:
    """Replay time in seconds for each packet, derived from packet_id.

    Gaps are preserved as real elapsed time — a dropped frame should replay as
    a frame of silence, not be closed up — but a backwards or absurd step is
    treated as one frame, since that means the counter restarted rather than
    that hours passed.
    """
    times: list[float] = []
    t = 0.0
    prev: int | None = None
    for p in packets:
        if prev is not None:
            step = p.packet_id - prev
            if step <= 0 or step > _MAX_SANE_ID_GAP:
                step = 1
            t += step / _TELEMETRY_RATE_HZ
        times.append(t)
        prev = p.packet_id
    return times


def replay(
    packets: Iterable[TelemetryPacket],
    cfg: AudioConfig,
    keep_audio: bool = False,
) -> ReplayResult:
    """Render a recorded session under `cfg` and measure the result.

    Deterministic: the same file and config always produce the same output, so
    a difference between two runs is a difference between two configs.
    """
    pkts = list(packets)
    result = ReplayResult(packets=len(pkts))
    if not pkts:
        return result

    bus = AudioBus(cfg)
    out = AudioOutput(bus)
    channels = out._out_channels
    block = out._block_size
    sr = cfg.sample_rate
    block_s = block / sr

    times = schedule(pkts)
    total_s = times[-1] + _TAIL_S
    n_blocks = max(1, int(math.ceil(total_s / block_s)))

    buf = np.zeros((block, channels), dtype=np.float32)
    # Accumulate rather than concatenate: a long session is hundreds of MB of
    # float32, and every statistic here is computable incrementally.
    peak = np.zeros(channels, dtype=np.float64)
    sumsq = np.zeros(channels, dtype=np.float64)
    clipped = 0
    limit_min = 1.0
    limit_active_blocks = 0
    kept: list[np.ndarray] = []

    nxt = 0
    for b in range(n_blocks):
        t_block = b * block_s
        while nxt < len(pkts) and times[nxt] <= t_block:
            bus.push_packet(pkts[nxt])
            nxt += 1

        buf[:] = 0.0
        out._callback(buf, block, None, None)

        peak = np.maximum(peak, np.abs(buf).max(axis=0))
        sumsq += np.square(buf, dtype=np.float64).sum(axis=0)
        clipped += int(np.count_nonzero(np.abs(buf) >= 0.9999))

        limit_min = min(limit_min, bus.limit_gain)
        if bus.limit_gain < _LIMIT_ACTIVE_GAIN:
            limit_active_blocks += 1

        if keep_audio:
            kept.append(buf.copy())

    frames = n_blocks * block
    result.frames = frames
    result.seconds = frames / sr
    result.channels = channels
    result.channel = [
        ChannelStats(peak=float(peak[i]), rms=float(math.sqrt(sumsq[i] / frames)))
        for i in range(channels)
    ]
    result.clipped_pct = 100.0 * clipped / (frames * channels)
    result.limiter_peak_db = _db(limit_min)
    result.limiter_duty_pct = 100.0 * limit_active_blocks / n_blocks
    if keep_audio:
        result.audio = np.concatenate(kept)
    return result


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """16-bit PCM, for listening to a replay or opening it in an editor.

    Bass shaker content is inaudible on speakers, but the waveform envelope is
    exactly what the eye is good at and what a spectrogram wants.
    """
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(audio.shape[1] if audio.ndim > 1 else 1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


@dataclass
class Comparison:
    label_a: str
    label_b: str
    a: ReplayResult
    b: ReplayResult

    def summary(self) -> str:
        lines = [
            f"A: {self.label_a}",
            self.a.summary(),
            "",
            f"B: {self.label_b}",
            self.b.summary(),
            "",
            "delta (B - A)",
        ]
        names = ("front", "rear")
        for i in range(min(len(self.a.channel), len(self.b.channel))):
            ca, cb = self.a.channel[i], self.b.channel[i]
            label = names[i] if i < len(names) and self.a.channels > 1 else f"ch{i}"
            lines.append(
                f"{label:>6}: rms {_ratio_db(cb.rms, ca.rms):+6.2f} dB   "
                f"crest {cb.crest_db - ca.crest_db:+6.2f} dB"
            )
        lines.append(
            f"limiter: duty {self.b.limiter_duty_pct - self.a.limiter_duty_pct:+.1f} pts   "
            f"peak {self.b.limiter_peak_db - self.a.limiter_peak_db:+.2f} dB"
        )
        lines.append("")
        lines.append(self.verdict())
        return "\n".join(lines)

    def verdict(self) -> str:
        """The interpretation, because a crest delta is not self-explanatory.

        Louder-and-flatter is the failure this whole thread has been circling:
        it reads as "more" in the seat while carrying less information, and the
        instinct it provokes is to turn it up again.
        """
        crest = _mean(
            [b.crest_db - a.crest_db for a, b in zip(self.a.channel, self.b.channel)]
        )
        rms = _mean(
            [_ratio_db(b.rms, a.rms) for a, b in zip(self.a.channel, self.b.channel)]
        )
        if abs(crest) < 0.25 and abs(rms) < 0.25:
            return "B is materially the same as A."
        if crest < -0.5 and rms > 0.25:
            return (
                "B is louder but flatter — dynamic range traded for level. This is the "
                "change that feels like more and conveys less."
            )
        if crest > 0.5:
            return "B has more dynamic range: transients stand further above the floor."
        if crest < -0.5:
            return "B is more compressed: transients sit closer to the continuous level."
        return f"B is {rms:+.2f} dB in level at essentially unchanged dynamics."


def _ratio_db(x: float, ref: float) -> float:
    if ref <= 0.0 or x <= 0.0:
        return 0.0
    return 20.0 * math.log10(x / ref)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def compare(
    packets: Sequence[TelemetryPacket],
    cfg_a: AudioConfig,
    cfg_b: AudioConfig,
    label_a: str = "A",
    label_b: str = "B",
) -> Comparison:
    """Render one session under two configs. The point of the whole exercise."""
    pkts = list(packets)
    return Comparison(label_a, label_b, replay(pkts, cfg_a), replay(pkts, cfg_b))
