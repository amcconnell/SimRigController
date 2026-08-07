import json
from pathlib import Path

import pytest

from shaker import profiles
from shaker.config import RIG_FIELDS, AudioConfig
from shaker.profiles import DEFAULT_PROFILE_NAME


@pytest.fixture
def tmp_state_path(tmp_path: Path) -> Path:
    return tmp_path / "profiles.json"


def test_load_missing_returns_empty_state(tmp_state_path: Path) -> None:
    state = profiles.load_state(tmp_state_path)
    assert state == {"active": DEFAULT_PROFILE_NAME, "profiles": {}}


def test_load_malformed_returns_empty_state(tmp_state_path: Path) -> None:
    tmp_state_path.write_text("not json")
    state = profiles.load_state(tmp_state_path)
    assert state == {"active": DEFAULT_PROFILE_NAME, "profiles": {}}


def test_save_load_roundtrip(tmp_state_path: Path) -> None:
    state = profiles.load_state(tmp_state_path)
    profiles.create(state, "GT3", AudioConfig(master_gain=0.7))
    profiles.save_state(state, tmp_state_path)

    reloaded = profiles.load_state(tmp_state_path)
    assert "GT3" in reloaded["profiles"]
    assert reloaded["profiles"]["GT3"]["master_gain"] == 0.7


def test_list_names_puts_default_first() -> None:
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {"B": {}, "A": {}}}
    assert profiles.list_names(state) == [DEFAULT_PROFILE_NAME, "B", "A"]


def test_get_audio_default_returns_code_defaults() -> None:
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {}}
    audio = profiles.get_audio(state, DEFAULT_PROFILE_NAME, AudioConfig())
    assert audio == AudioConfig()


def test_get_audio_unknown_raises() -> None:
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {}}
    with pytest.raises(KeyError):
        profiles.get_audio(state, "missing", AudioConfig())


def test_get_audio_ignores_unknown_fields() -> None:
    # Simulate older stored profile with a field that's been removed.
    state = {
        "active": DEFAULT_PROFILE_NAME,
        "profiles": {"P": {"master_gain": 0.5, "ghost_field_removed": 999}},
    }
    audio = profiles.get_audio(state, "P", AudioConfig())
    assert audio.master_gain == 0.5


def test_create_rejects_reserved_name() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    with pytest.raises(ValueError):
        profiles.create(state, DEFAULT_PROFILE_NAME, AudioConfig())


def test_create_rejects_empty_name() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    with pytest.raises(ValueError):
        profiles.create(state, "   ", AudioConfig())


def test_create_rejects_duplicate() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    profiles.create(state, "X", AudioConfig())
    with pytest.raises(ValueError):
        profiles.create(state, "X", AudioConfig())


def test_delete_default_raises() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    with pytest.raises(ValueError):
        profiles.delete(state, DEFAULT_PROFILE_NAME)


def test_delete_falls_back_to_default() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    profiles.create(state, "X", AudioConfig())
    state["active"] = "X"
    profiles.delete(state, "X")
    assert state["active"] == DEFAULT_PROFILE_NAME


def test_rename_updates_active_pointer() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    profiles.create(state, "Old", AudioConfig())
    state["active"] = "Old"
    profiles.rename(state, "Old", "New")
    assert "New" in state["profiles"]
    assert "Old" not in state["profiles"]
    assert state["active"] == "New"


def test_rename_preserves_order() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    for n in ("A", "B", "C"):
        profiles.create(state, n, AudioConfig())
    profiles.rename(state, "B", "BB")
    assert list(state["profiles"].keys()) == ["A", "BB", "C"]


def test_update_active_audio_noop_for_default() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    profiles.create(state, "X", AudioConfig(master_gain=0.1))
    state["active"] = DEFAULT_PROFILE_NAME
    profiles.update_active_audio(state, AudioConfig(master_gain=0.99))
    # Default isn't stored; X must remain its original value.
    assert state["profiles"]["X"]["master_gain"] == 0.1


def test_update_active_audio_writes_to_active_profile() -> None:
    state = profiles.load_state(Path("/tmp/__never_exists__"))
    profiles.create(state, "X", AudioConfig(master_gain=0.1))
    state["active"] = "X"
    profiles.update_active_audio(state, AudioConfig(master_gain=0.85))
    assert state["profiles"]["X"]["master_gain"] == 0.85


def test_save_state_is_atomic(tmp_state_path: Path) -> None:
    """Atomic replace: no .tmp file should remain after a successful save."""
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {"P": {}}}
    profiles.save_state(state, tmp_state_path)
    assert tmp_state_path.exists()
    assert not tmp_state_path.with_suffix(".json.tmp").exists()
    # File is valid JSON.
    json.loads(tmp_state_path.read_text())


# --- Rig fields vs taste ---------------------------------------------------
#
# A profile is taste: what an event should feel like. The rig fields describe
# the machine, and before the split they rode along in every snapshot — so
# activating a profile re-litigated the hardware, and a published profile would
# have tried to reconfigure someone else's sound card.


def _rig() -> AudioConfig:
    """A rig that looks nothing like the shipped defaults on every rig field."""
    return AudioConfig(
        device="USB Advanced Audio Device",
        sample_rate=44100,
        buffer_ms=40,
        output_channels=2,
        rear_gain_trim=2.0,
    )


def test_created_profile_stores_no_rig_fields() -> None:
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {}}
    profiles.create(state, "GT3", _rig())
    stored = state["profiles"]["GT3"]
    assert not (RIG_FIELDS & stored.keys()), sorted(RIG_FIELDS & stored.keys())
    # ...but everything else is still there.
    assert stored["master_gain"] == AudioConfig().master_gain
    assert "vibration_gain" in stored


def test_activating_a_profile_leaves_the_rig_alone() -> None:
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {}}
    profiles.create(state, "GT3", AudioConfig(master_gain=0.9))
    audio = profiles.get_audio(state, "GT3", _rig())
    assert audio.master_gain == 0.9           # taste came from the profile
    for f in RIG_FIELDS:
        assert getattr(audio, f) == getattr(_rig(), f), f


def test_reverting_to_default_leaves_the_rig_alone() -> None:
    """The case that actually bit: `default` used to reset a stereo rig to mono."""
    audio = profiles.get_audio({"profiles": {}}, DEFAULT_PROFILE_NAME, _rig())
    assert audio.output_channels == 2
    assert audio.device == "USB Advanced Audio Device"
    assert audio.rear_gain_trim == 2.0
    # Taste still reverts, which is the whole point of activating default.
    assert audio.master_gain == AudioConfig().master_gain


def test_legacy_profile_with_rig_fields_is_ignored_not_applied() -> None:
    """Profiles written before the split still carry the keys on disk."""
    state = {
        "active": DEFAULT_PROFILE_NAME,
        "profiles": {
            "old": {
                "master_gain": 0.4,
                "device": "some other card",
                "output_channels": 1,
                "rear_gain_trim": 0.25,
            }
        },
    }
    audio = profiles.get_audio(state, "old", _rig())
    assert audio.master_gain == 0.4
    assert audio.device == "USB Advanced Audio Device"
    assert audio.output_channels == 2
    assert audio.rear_gain_trim == 2.0


def test_saving_over_a_legacy_profile_drops_the_rig_keys() -> None:
    state = {
        "active": "old",
        "profiles": {"old": {"master_gain": 0.4, "device": "some other card"}},
    }
    profiles.update_active_audio(state, _rig())
    assert not (RIG_FIELDS & state["profiles"]["old"].keys())


def test_round_trip_through_a_profile_preserves_every_taste_field() -> None:
    """Nothing outside RIG_FIELDS may be lost by storing and reloading."""
    tuned = AudioConfig(
        master_gain=0.33, vibration_gamma=1.7, engine_rumble_bias=-0.4,
        wheel_slip_lock_freq_hz=51.0, gear_shift_duration_ms=120,
    )
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {}}
    profiles.create(state, "P", tuned)
    back = profiles.get_audio(state, "P", tuned)
    assert back == tuned
