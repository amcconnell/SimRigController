"""Named audio-config profiles.

A "profile" is a snapshot of [AudioConfig][shaker.config.AudioConfig] under a
human-readable name. The special `default` profile is provided by code
(`AudioConfig()` defaults) and is not stored — it can be activated and
reverted to, but never edited or deleted. User-created profiles live in
`config/profiles.json` alongside an `active` pointer.

Activating a profile copies its audio fields into the live `shaker.toml`
[audio] section; the file watcher then reloads as usual. Editing audio while
a non-default profile is active also writes the changes back to that
profile, so per-profile tweaks survive restarts and re-activations.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from shaker.config import AudioConfig, Config

DEFAULT_PROFILE_NAME = "default"
PROFILES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "profiles.json"

log = logging.getLogger(__name__)


def _empty_state() -> dict[str, Any]:
    return {"active": DEFAULT_PROFILE_NAME, "profiles": {}}


def load_state(path: Path = PROFILES_PATH) -> dict[str, Any]:
    """Read the profiles file; return an empty default state if missing or malformed."""
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or "profiles" not in data:
            log.warning("profiles file at %s malformed; using empty state", path)
            return _empty_state()
        data.setdefault("active", DEFAULT_PROFILE_NAME)
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("failed to read profiles file (%s); using empty state", exc)
        return _empty_state()


def save_state(state: dict[str, Any], path: Path = PROFILES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def list_names(state: dict[str, Any]) -> list[str]:
    """Default first, then user-created profiles in stable order."""
    return [DEFAULT_PROFILE_NAME, *state.get("profiles", {}).keys()]


def get_audio(state: dict[str, Any], name: str) -> AudioConfig:
    """Resolve a profile name to its AudioConfig. Default → code-shipped defaults."""
    if name == DEFAULT_PROFILE_NAME:
        return AudioConfig()
    profiles = state.get("profiles", {})
    if name not in profiles:
        raise KeyError(f"unknown profile: {name!r}")
    stored = profiles[name]
    if not isinstance(stored, dict):
        raise ValueError(f"profile {name!r} is not a dict")
    # Filter unknown keys so schema additions (new audio fields) survive.
    fields = {f.name for f in AudioConfig.__dataclass_fields__.values()}
    return AudioConfig(**{k: v for k, v in stored.items() if k in fields})


def create(
    state: dict[str, Any],
    name: str,
    source_audio: AudioConfig,
) -> dict[str, Any]:
    """Create a new profile from the given audio config (a clone)."""
    name = name.strip()
    if not name:
        raise ValueError("profile name cannot be empty")
    if name == DEFAULT_PROFILE_NAME:
        raise ValueError(f"profile name {DEFAULT_PROFILE_NAME!r} is reserved")
    profiles = state.setdefault("profiles", {})
    if name in profiles:
        raise ValueError(f"profile {name!r} already exists")
    profiles[name] = asdict(source_audio)
    return state


def delete(state: dict[str, Any], name: str) -> dict[str, Any]:
    if name == DEFAULT_PROFILE_NAME:
        raise ValueError("cannot delete the default profile")
    profiles = state.get("profiles", {})
    if name not in profiles:
        raise KeyError(f"unknown profile: {name!r}")
    del profiles[name]
    if state.get("active") == name:
        state["active"] = DEFAULT_PROFILE_NAME
    return state


def rename(state: dict[str, Any], old_name: str, new_name: str) -> dict[str, Any]:
    new_name = new_name.strip()
    if old_name == DEFAULT_PROFILE_NAME:
        raise ValueError("cannot rename the default profile")
    if not new_name:
        raise ValueError("new name cannot be empty")
    if new_name == DEFAULT_PROFILE_NAME:
        raise ValueError(f"profile name {DEFAULT_PROFILE_NAME!r} is reserved")
    profiles = state.get("profiles", {})
    if old_name not in profiles:
        raise KeyError(f"unknown profile: {old_name!r}")
    if new_name in profiles:
        raise ValueError(f"profile {new_name!r} already exists")
    # Preserve insertion order: rebuild the dict.
    state["profiles"] = {
        (new_name if k == old_name else k): v
        for k, v in profiles.items()
    }
    if state.get("active") == old_name:
        state["active"] = new_name
    return state


def update_active_audio(state: dict[str, Any], audio: AudioConfig) -> dict[str, Any]:
    """Persist an audio config into the active non-default profile.

    No-op when the active profile is `default` — default is code-shipped and
    never written back.
    """
    active = state.get("active", DEFAULT_PROFILE_NAME)
    if active == DEFAULT_PROFILE_NAME:
        return state
    profiles = state.setdefault("profiles", {})
    if active not in profiles:
        # Stale active pointer; the caller is editing default-ish state.
        return state
    profiles[active] = asdict(audio)
    return state


def apply_to_live_config(audio: AudioConfig, live: Config) -> Config:
    """Build a new Config with the given audio section, preserving gt7/web."""
    return Config(gt7=live.gt7, web=live.web, audio=audio)
