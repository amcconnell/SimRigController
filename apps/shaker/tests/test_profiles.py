import json
from pathlib import Path

import pytest

from shaker import profiles
from shaker.config import AudioConfig
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
    audio = profiles.get_audio(state, DEFAULT_PROFILE_NAME)
    assert audio == AudioConfig()


def test_get_audio_unknown_raises() -> None:
    state = {"active": DEFAULT_PROFILE_NAME, "profiles": {}}
    with pytest.raises(KeyError):
        profiles.get_audio(state, "missing")


def test_get_audio_ignores_unknown_fields() -> None:
    # Simulate older stored profile with a field that's been removed.
    state = {
        "active": DEFAULT_PROFILE_NAME,
        "profiles": {"P": {"master_gain": 0.5, "ghost_field_removed": 999}},
    }
    audio = profiles.get_audio(state, "P")
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
