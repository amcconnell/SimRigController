"""Swept-frequency measurement: the detector, the tone, and the sweep.

The point of this measurement is to find a resonance, so the test that matters
is that a rig with a known resonance comes back with that frequency. Everything
else here protects the detector, because a narrowband detector that quietly
mis-tunes reports silence from a working shaker — a failure that looks exactly
like a mechanical fault and would send someone under the rig with a spanner.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time

import numpy as np
import pytest

from shaker.audio.bus import AudioBus
from shaker.audio.stream import AudioOutput
from shaker.config import AudioConfig
from shaker.sensors.adxl345 import ADDR_ALT, ADDR_PRIMARY
from shaker.sensors.pods import _CAPTURE_MAX, SensorHub
from shaker.sensors.sweep import (
    SweepPoint,
    SweepTask,
    band_isolation,
    frequencies,
    isolation_curve,
    run,
    summarise,
)
from shaker.sensors.tone import detect

from test_sensors import FakeBus

RATE = 800.0
FRAMES = 960
FRONT, REAR = 0, 1


def _samples(freq: float, amp: float, n: int, rate: float = RATE,
             noise: float = 0.0, seed: int = 1) -> tuple[list, list, list]:
    """A sine on Z, the axis a pod bolted flat actually sees."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / rate
    z = amp * np.sin(2 * np.pi * freq * t)
    if noise:
        z = z + rng.normal(0.0, noise, n)
    zeros = [0.0] * n
    return (zeros, list(zeros), list(z))


# --- the detector ----------------------------------------------------------


def test_detect_recovers_a_known_amplitude_as_rms() -> None:
    x, y, z = _samples(40.0, 0.2, 800)
    amp, found = detect(x, y, z, RATE, 40.0)
    # Reported as RMS so it sits on the same scale as every other figure.
    assert amp == pytest.approx(0.2 / math.sqrt(2), rel=0.05)
    assert found == pytest.approx(40.0, rel=0.02)


def test_detect_combines_axes_in_power() -> None:
    """Orientation-independent: the same motion split across two axes reads the same."""
    n = 800
    t = np.arange(n) / RATE
    wave = 0.2 * np.sin(2 * np.pi * 40.0 * t)
    one = detect([0.0] * n, [0.0] * n, list(wave), RATE, 40.0)[0]
    k = 1 / math.sqrt(2)
    split = detect(list(wave * k), [0.0] * n, list(wave * k), RATE, 40.0)[0]
    assert split == pytest.approx(one, rel=0.02)


@pytest.mark.parametrize("told_rate", [820.0, 772.0])
def test_detect_survives_the_rate_error_the_real_pods_produce(told_rate: float) -> None:
    """A pod's reported rate wanders; the detector must not care.

    Measured on the rig: the front pod reported 772-789 Hz over twelve seconds
    and the rear 784-820, because the figure is counted over about a second and
    quantised by FIFO batching. That wander reaches this detector as the sample
    rate. A lock-in that believed it would rotate through a full turn during the
    window at the top of the band and average the tone away — reporting a dead
    shaker from a working one, which is the expensive failure here because it
    sends someone under the rig with a spanner. The frequency search is what
    prevents it, and this pins the search to what the hardware actually does.
    """
    x, y, z = _samples(110.0, 0.2, 800, rate=RATE)
    amp, _ = detect(x, y, z, told_rate, 110.0)
    assert amp == pytest.approx(0.2 / math.sqrt(2), rel=0.12)


def test_detect_rejects_a_tone_at_another_frequency() -> None:
    x, y, z = _samples(90.0, 0.5, 800)
    amp, _ = detect(x, y, z, RATE, 40.0)
    assert amp < 0.5 * 0.05, "a 90 Hz tone leaked into the 40 Hz detector"


def test_detect_pulls_a_tone_out_of_louder_noise() -> None:
    """The whole reason this exists: the guard now refuses broadband readings."""
    amp_g = 0.05
    x, y, z = _samples(40.0, amp_g, 1600, noise=0.10)
    broadband = float(np.sqrt(np.mean(np.square(z))))
    narrow, _ = detect(x, y, z, RATE, 40.0)
    assert broadband > 0.09, "test noise too quiet to prove anything"
    assert narrow == pytest.approx(amp_g / math.sqrt(2), rel=0.25)


def test_detect_refuses_above_nyquist() -> None:
    x, y, z = _samples(40.0, 0.2, 800)
    assert detect(x, y, z, RATE, 395.0)[0] == 0.0


def test_detect_refuses_a_window_too_short_to_mean_anything() -> None:
    x, y, z = _samples(40.0, 0.2, 16)
    assert detect(x, y, z, RATE, 40.0)[0] == 0.0


# --- the frequency list ----------------------------------------------------


def test_frequencies_are_geometric_and_hit_both_ends() -> None:
    f = frequencies(15.0, 120.0, 6)
    assert f[0] == pytest.approx(15.0)
    assert f[-1] == pytest.approx(120.0, rel=0.01)
    ratios = [b / a for a, b in zip(f, f[1:])]
    assert all(r == pytest.approx(2 ** (1 / 6), rel=0.01) for r in ratios)


# --- pod capture -----------------------------------------------------------


def _hub_with_pods() -> tuple[SensorHub, FakeBus]:
    fake = FakeBus(addresses=(ADDR_PRIMARY, ADDR_ALT))
    hub = SensorHub()
    hub.start(bus=fake)
    return (hub, fake)


def test_capture_is_consumed_so_a_stale_window_cannot_be_reread() -> None:
    hub, fake = _hub_with_pods()
    try:
        pod = hub._pods[0]
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not pod.stats.present:
            time.sleep(0.02)
        pod.begin_capture()
        for _ in range(40):
            fake.push(ADDR_PRIMARY, 0.0, 0.0, 1.0)
        time.sleep(0.15)
        x, _, _, _ = pod.end_capture()
        assert x, "capture returned nothing while samples were arriving"
        again, _, _, _ = pod.end_capture()
        assert again == [], "a second read handed back the previous measurement"
    finally:
        hub.stop()


def test_capture_is_bounded_when_nothing_closes_it() -> None:
    """An accumulator left open by a failed measurement must not grow forever."""
    hub, fake = _hub_with_pods()
    try:
        pod = hub._pods[0]
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not pod.stats.present:
            time.sleep(0.02)
        pod.begin_capture()
        stop = time.monotonic() + 2.0
        while time.monotonic() < stop:
            for _ in range(32):
                fake.push(ADDR_PRIMARY, 0.0, 0.0, 1.0)
            time.sleep(0.01)
        # Never closed. The cap is per batch, so allow one batch of overshoot.
        assert len(pod._cap_x) <= _CAPTURE_MAX + 64
    finally:
        hub.stop()


# --- the tone in the audio path --------------------------------------------


def _cfg() -> AudioConfig:
    return AudioConfig(output_channels=2, buffer_ms=20)


def test_tone_plays_on_one_channel_only() -> None:
    bus = AudioBus(_cfg())
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_tone(45.0, REAR, 0.5)
    out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf[:, FRONT])) == 0.0
    assert np.max(np.abs(buf[:, REAR])) > 0.05


def test_tone_is_at_the_frequency_asked_for() -> None:
    bus = AudioBus(_cfg())
    out = AudioOutput(bus)
    rate = bus.audio_config.sample_rate
    n = 8192
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_tone(60.0, FRONT, 2.0)
    rendered = []
    while sum(len(b) for b in rendered) < n:
        out._callback(buf, FRAMES, None, None)
        rendered.append(buf[:, FRONT].copy())
    sig = np.concatenate(rendered)[:n]
    spectrum = np.abs(np.fft.rfft(sig * np.hanning(n)))
    peak_hz = float(np.fft.rfftfreq(n, 1 / rate)[int(np.argmax(spectrum))])
    assert peak_hz == pytest.approx(60.0, abs=2.0)


def test_tone_respects_mute() -> None:
    bus = AudioBus(_cfg())
    bus.muted = True
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_tone(45.0, REAR, 0.5)
    out._callback(buf, FRAMES, None, None)
    assert np.max(np.abs(buf)) == 0.0


def test_a_wiring_check_is_not_cut_short_by_a_tone() -> None:
    """Both replace the whole mix; the hand-started one has to win."""
    bus = AudioBus(_cfg())
    out = AudioOutput(bus)
    buf = np.zeros((FRAMES, 2), dtype=np.float32)
    bus.trigger_wiring_check(pulse_s=0.5, gap_s=0.2)
    bus.trigger_tone(90.0, REAR, 0.5)
    out._callback(buf, FRAMES, None, None)
    # The wiring check's first pulse is front-only; the tone asked for rear.
    assert np.max(np.abs(buf[:, REAR])) == 0.0
    assert np.max(np.abs(buf[:, FRONT])) > 0.05


# --- the sweep end to end --------------------------------------------------


class ResonantRig:
    """A rig whose front/rear coupling has a known natural frequency.

    Transmissibility of a damped single-degree-of-freedom isolator, which is
    what four rubber mounts under a seat are to a first approximation. If the
    sweep can find this peak it can find a real one.
    """

    def __init__(self, bus: AudioBus, fake: FakeBus, fn_hz: float, zeta: float = 0.15,
                 drive_g: float = 0.4, noise_g: float = 0.0) -> None:
        self.bus, self.fake = bus, fake
        self.fn_hz, self.zeta = fn_hz, zeta
        self.drive_g, self.noise_g = drive_g, noise_g
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._seen = 0
        self._started: float | None = None
        self._phase = 0.0
        self._rng = np.random.default_rng(7)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        self._t.join(timeout=1.0)

    def transmissibility(self, freq: float) -> float:
        r = freq / self.fn_hz
        num = 1.0 + (2.0 * self.zeta * r) ** 2
        den = (1.0 - r * r) ** 2 + (2.0 * self.zeta * r) ** 2
        return math.sqrt(num / den)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.bus.tone_count != self._seen:
                self._seen = self.bus.tone_count
                self._started = time.monotonic()
            playing = (
                self._started is not None
                and time.monotonic() - self._started < self.bus.tone_s
            )
            if playing:
                freq = self.bus.tone_freq_hz
                near = self.drive_g
                far = self.drive_g * self.transmissibility(freq)
                if self.bus.tone_channel == FRONT:
                    f_amp, r_amp = near, far
                else:
                    f_amp, r_amp = far, near
            else:
                freq, f_amp, r_amp = 40.0, 0.0, 0.0
            step = 2 * math.pi * freq / RATE
            for _ in range(16):
                # Phase accumulates rather than being recomputed, so a change of
                # frequency does not inject a step the detector would see as a
                # click at every frequency at once.
                self._phase = (self._phase + step) % (2 * math.pi)
                s = math.sin(self._phase)
                n1 = self._rng.normal(0.0, self.noise_g) if self.noise_g else 0.0
                n2 = self._rng.normal(0.0, self.noise_g) if self.noise_g else 0.0
                self.fake.push(ADDR_PRIMARY, 0.0, 0.0, 1.0 + f_amp * s + n1)
                self.fake.push(ADDR_ALT, 0.0, 0.0, 1.0 + r_amp * s + n2)
            time.sleep(0.02)


_FAST = dict(settle_s=0.05, window_s=0.25, gap_s=0.02, ambient_repeats=1)


def _resonant(fn_hz: float, **kw):
    abus = AudioBus(_cfg())
    fake = FakeBus(addresses=(ADDR_PRIMARY, ADDR_ALT))
    hub = SensorHub()
    hub.start(bus=fake)
    rig = ResonantRig(abus, fake, fn_hz, **kw)
    rig.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not all(p.stats.present for p in hub._pods):
        time.sleep(0.02)
    time.sleep(0.6)   # let the gravity tracker settle
    return (abus, hub, rig)


def test_the_sweep_finds_a_known_resonance() -> None:
    """The whole reason the sweep exists: read fn off the curve, don't infer it."""
    abus, hub, rig = _resonant(25.0)
    try:
        result = asyncio.run(run(abus, hub, freqs=[18.0, 25.0, 35.0, 55.0, 90.0], **_FAST))
    finally:
        rig.stop()
        hub.stop()

    assert result.ok, result.reason
    assert result.summary["worst_hz"] == pytest.approx(25.0, abs=0.01), result.summary
    # Above resonance an isolator isolates; the curve has to show that.
    curve = {c["freq_hz"]: c["isolation_db"] for c in result.summary["curve"]}
    assert curve[90.0] < curve[25.0] - 6.0, curve


def test_a_silent_shaker_is_refused_rather_than_reported() -> None:
    """A dead channel must not come back as a ratio someone could act on.

    Deliberately a rig that emits nothing at all rather than one buried in
    noise: at the fast timings these tests use, the window is 0.25 s and the
    detector bandwidth about 4 Hz, so a narrowband noise estimate has a
    time-bandwidth product near one and swings by its own size. Comparing two
    such draws against a 3x threshold would decide this test by coin toss.
    Production integrates for 0.8 s and averages three ambient captures, which
    is what makes the guard meaningful there.
    """
    abus, hub, rig = _resonant(25.0, drive_g=0.0, noise_g=0.0)
    try:
        result = asyncio.run(run(abus, hub, freqs=[40.0], **_FAST))
    finally:
        rig.stop()
        hub.stop()
    assert all(not p.ok for p in result.points)
    assert all(p.ratio_db is None for p in result.points)
    assert all("noise floor" in (p.note or "") for p in result.points)
    assert not result.ok and result.reason


# --- the derived numbers ---------------------------------------------------


def _pt(freq, drive, near, far, ok=True, floor=False) -> SweepPoint:
    p = SweepPoint(freq_hz=freq, drive=drive, near_g=near, far_g=far)
    p.ok = ok
    p.floor_limited = floor
    p.ratio_db = 20 * math.log10(far / near) if ok and near else None
    return p


def test_isolation_pairs_the_two_directions() -> None:
    points = [_pt(40.0, "front", 0.2, 0.05), _pt(40.0, "rear", 0.2, 0.1)]
    curve = isolation_curve(points)
    assert curve[0]["front_to_rear_db"] == pytest.approx(-12.0, abs=0.1)
    assert curve[0]["rear_to_front_db"] == pytest.approx(-6.0, abs=0.1)
    # The mean, which is immune to the two pods being mismatched.
    assert curve[0]["isolation_db"] == pytest.approx(-9.0, abs=0.1)


def test_one_missing_direction_leaves_isolation_unreported() -> None:
    curve = isolation_curve([_pt(40.0, "front", 0.2, 0.05)])
    assert curve[0]["isolation_db"] is None


def test_a_floor_limited_point_is_excluded_from_the_worst_case() -> None:
    """Otherwise the noise floor, which rises at low frequency, reads as a peak."""
    points = [
        _pt(20.0, "front", 0.2, 0.19, floor=True), _pt(20.0, "rear", 0.2, 0.19, floor=True),
        _pt(40.0, "front", 0.2, 0.1), _pt(40.0, "rear", 0.2, 0.1),
        _pt(80.0, "front", 0.2, 0.01), _pt(80.0, "rear", 0.2, 0.01),
    ]
    curve = isolation_curve(points)
    summary = summarise(points, curve)
    assert summary["worst_hz"] == 40.0
    assert summary["best_hz"] == 80.0
    assert summary["spread_db"] == pytest.approx(20.0, abs=0.1)


def test_band_isolation_averages_only_inside_the_band() -> None:
    curve = [
        {"freq_hz": 30.0, "isolation_db": -2.0},
        {"freq_hz": 45.0, "isolation_db": -10.0},
        {"freq_hz": 48.0, "isolation_db": -12.0},
        {"freq_hz": 70.0, "isolation_db": -20.0},
    ]
    assert band_isolation(curve, 44.0, 50.0) == pytest.approx(-11.0, abs=0.1)
    assert band_isolation(curve, 200.0, 300.0) is None


# --- the background task ---------------------------------------------------


def test_a_second_sweep_is_refused_rather_than_interleaved() -> None:
    """Two sweeps at once would each measure the other's tones."""
    abus, hub, rig = _resonant(25.0)

    async def go() -> tuple[bool, bool]:
        task = SweepTask()
        first = task.start(abus, hub, freqs=[40.0], **_FAST)
        second = task.start(abus, hub, freqs=[40.0], **_FAST)
        while task.running:
            await asyncio.sleep(0.05)
        return (first, second)

    try:
        first, second = asyncio.run(go())
    finally:
        rig.stop()
        hub.stop()
    assert first and not second


def test_the_task_reports_progress_and_survives_a_failure() -> None:
    class Broken:
        _pods: list = []

    async def go() -> dict:
        task = SweepTask()
        task.start(AudioBus(_cfg()), Broken(), freqs=[40.0], **_FAST)
        while task.running:
            await asyncio.sleep(0.02)
        return task.status()

    status = asyncio.run(go())
    assert not status["running"]
    # A hub with no pods is a refusal, not a crash — absence is a state here.
    assert status["error"] is None
    assert status["result"]["reason"]
