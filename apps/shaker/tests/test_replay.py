"""Offline replay of a recorded session.

The two properties worth protecting: replay is *deterministic*, or an A/B is
comparing noise, and it measures the *real* DSP rather than a model of it — the
harness drives AudioOutput._callback and AudioBus.push_packet directly, so a
change to either shows up here.
"""

from __future__ import annotations

import math
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from shaker.config import AudioConfig
from shaker.gt7.protocol import TelemetryPacket
from shaker.recording import SessionRecorder, read_session
from shaker.replay import compare, replay, schedule, write_wav

# is_on_track (bit 0) set, not paused — the gate push_packet applies.
_DRIVING = 0b01


def _lap(n: int = 240, rough: bool = True) -> list[TelemetryPacket]:
    """A synthetic lap: bumps, revs, and a lockup-then-spin under braking."""
    out = []
    for i in range(n):
        phase = i / 30.0
        bump = 0.02 * math.sin(phase * 7.0) if rough else 0.0
        out.append(TelemetryPacket(
            packet_id=1000 + i,
            flags=_DRIVING,
            lap_count=1,
            speed_mps=40.0 + 15.0 * math.sin(phase),
            engine_rpm=5000.0 + 2000.0 * abs(math.sin(phase)),
            max_alert_rpm=7200,
            throttle=180 if i % 60 < 40 else 0,
            brake=0 if i % 60 < 40 else 200,
            current_gear=3 + (i // 60) % 3,
            suspension_FL=0.10 + bump,
            suspension_FR=0.10 - bump,
            suspension_RL=0.10 + bump * 0.6,
            suspension_RR=0.10 - bump * 0.6,
            wheel_rps_FL=130.0, wheel_rps_FR=130.0,
            wheel_rps_RL=140.0, wheel_rps_RR=140.0,
            tire_radius_FL=0.317, tire_radius_FR=0.317,
            tire_radius_RL=0.317, tire_radius_RR=0.317,
        ))
    return out


def test_replay_is_deterministic() -> None:
    """Two runs of one file must be bit-identical, or an A/B measures nothing."""
    lap, cfg = _lap(), AudioConfig()
    a = replay(lap, cfg, keep_audio=True)
    b = replay(lap, cfg, keep_audio=True)
    assert a.audio is not None and b.audio is not None
    assert np.array_equal(a.audio, b.audio)
    assert a.channel[0].rms == b.channel[0].rms


def test_replay_renders_actual_signal() -> None:
    lap = _lap()
    r = replay(lap, AudioConfig())
    assert r.packets == len(lap)
    assert r.seconds > 3.0
    assert r.channel[0].peak > 0.0
    assert r.channel[0].rms > 0.0


def test_silence_in_gives_silence_out() -> None:
    """Menu frames are recorded; they must not render as anything."""
    menu = [TelemetryPacket(packet_id=i, lap_count=-1) for i in range(120)]
    r = replay(menu, AudioConfig())
    assert r.channel[0].peak == 0.0
    assert r.limiter_duty_pct == 0.0


def test_schedule_preserves_dropped_frames_as_elapsed_time() -> None:
    packets = [TelemetryPacket(packet_id=i) for i in (10, 11, 40, 41)]
    times = schedule(packets)
    assert times[0] == 0.0
    assert times[1] == pytest.approx(1 / 60)
    # 29 frames skipped is 29 frames of real time, not a closed-up gap.
    assert times[2] == pytest.approx(30 / 60)
    assert times[3] == pytest.approx(31 / 60)


def test_schedule_treats_a_counter_restart_as_one_frame() -> None:
    """A backwards or absurd jump means a new session, not hours of silence."""
    packets = [TelemetryPacket(packet_id=i) for i in (5000, 5001, 3, 4)]
    times = schedule(packets)
    assert times[-1] == pytest.approx(3 / 60)

    forward = schedule([TelemetryPacket(packet_id=i) for i in (1, 2, 999999)])
    assert forward[-1] == pytest.approx(2 / 60)


def test_master_gain_raises_level_and_costs_crest() -> None:
    """The failure this whole tool exists to make visible.

    Driven far past the ceiling the limiter stops protecting transients and
    starts setting the level: RMS climbs while crest — peak over RMS — falls.
    Peak alone cannot show it, because the limiter pins peak by construction.
    """
    lap = _lap()
    quiet = replay(lap, AudioConfig(master_gain=0.5))
    loud = replay(lap, AudioConfig(master_gain=8.0))

    assert loud.channel[0].rms > quiet.channel[0].rms
    assert loud.channel[0].crest_db < quiet.channel[0].crest_db
    assert loud.limiter_duty_pct > quiet.limiter_duty_pct


def test_limiter_holds_the_output_below_full_scale() -> None:
    """However hard it is driven, the render must not clip."""
    r = replay(_lap(), AudioConfig(master_gain=20.0))
    assert r.clipped_pct == 0.0, r.summary()
    assert r.channel[0].peak <= 1.0


def test_stereo_replays_two_channels() -> None:
    r = replay(_lap(), AudioConfig(output_channels=2))
    assert r.channels == 2
    assert len(r.channel) == 2
    assert r.channel[0].rms > 0.0 and r.channel[1].rms > 0.0


# Below this the mix stays under the limiter's ceiling for this synthetic lap,
# so each channel can be reasoned about on its own. At the shipped default the
# limiter is engaged and the channels are coupled — see the test below.
_UNLIMITED_GAIN = 0.1


def test_rear_trim_leaves_the_front_alone_when_not_limiting() -> None:
    flat = replay(_lap(), AudioConfig(
        output_channels=2, rear_gain_trim=1.0, master_gain=_UNLIMITED_GAIN))
    trimmed = replay(_lap(), AudioConfig(
        output_channels=2, rear_gain_trim=0.5, master_gain=_UNLIMITED_GAIN))
    assert flat.limiter_duty_pct == 0.0, "premise: the limiter must be idle here"
    assert trimmed.channel[1].rms < flat.channel[1].rms
    assert trimmed.channel[0].rms == pytest.approx(flat.channel[0].rms, rel=1e-9)


def test_rear_trim_does_move_the_front_once_the_limiter_engages() -> None:
    """A real coupling, and a surprising one — found by this harness.

    The limiter derives one gain from the loudest channel and applies it to
    both, deliberately, so front/rear balance does not shift under load. The
    consequence is that turning the rear DOWN takes the loudest channel with
    it, the shared gain relaxes, and the FRONT gets louder. Rear trim is
    therefore not an independent control whenever the rig is limiting, which
    is worth knowing before chasing a front/rear balance by ear.
    """
    loud = AudioConfig(output_channels=2, master_gain=4.0)
    flat = replay(_lap(), replace(loud, rear_gain_trim=1.0))
    trimmed = replay(_lap(), replace(loud, rear_gain_trim=0.5))
    assert flat.limiter_duty_pct > 50.0, "premise: the limiter must be working"
    assert trimmed.channel[0].rms > flat.channel[0].rms


def test_disabling_an_effect_lowers_the_result() -> None:
    """Proves the harness is driving the real chain, not a stand-in."""
    full = replay(_lap(), AudioConfig(master_gain=_UNLIMITED_GAIN))
    without = replay(_lap(), AudioConfig(
        master_gain=_UNLIMITED_GAIN, vibration_enabled=False))
    assert full.limiter_duty_pct == 0.0
    assert without.channel[0].rms < full.channel[0].rms


def test_compare_reports_a_delta_and_a_verdict() -> None:
    lap = _lap()
    c = compare(lap, AudioConfig(master_gain=0.5), AudioConfig(master_gain=8.0),
                label_a="quiet", label_b="loud")
    text = c.summary()
    assert "quiet" in text and "loud" in text
    assert "crest" in text
    assert "flatter" in c.verdict(), c.verdict()


def test_compare_calls_identical_configs_the_same() -> None:
    lap = _lap()
    c = compare(lap, AudioConfig(), AudioConfig())
    assert "materially the same" in c.verdict()


def test_round_trip_from_a_real_recording(tmp_path: Path) -> None:
    """Recorder and replay have to agree end to end, not just in isolation."""
    r = SessionRecorder(tmp_path / "rec")
    path = r.start()
    for p in _lap(120):
        r.on_packet(p)
    r.stop()

    _, packets = read_session(path)
    result = replay(packets, AudioConfig())
    assert result.packets == 120
    assert result.channel[0].rms > 0.0


def test_empty_session_does_not_crash() -> None:
    result = replay([], AudioConfig())
    assert result.packets == 0
    assert result.seconds == 0.0


def test_write_wav_is_readable(tmp_path: Path) -> None:
    r = replay(_lap(60), AudioConfig(output_channels=2), keep_audio=True)
    assert r.audio is not None
    out = tmp_path / "lap.wav"
    write_wav(out, r.audio, 48000)

    with wave.open(str(out)) as w:
        assert w.getnchannels() == 2
        assert w.getframerate() == 48000
        assert w.getsampwidth() == 2
        assert w.getnframes() == r.frames
