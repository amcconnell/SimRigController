"""Config load/save round-trips, schema mirrors, and rollback safety."""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from shaker import config as cfg_mod
from shaker.config import (
    RESTART_REQUIRED_FIELDS,
    AudioConfig,
    Config,
    GT7Config,
    WebConfig,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND_TYPES = _REPO_ROOT / "frontend" / "src" / "types" / "config.ts"
_APP_TSX = _REPO_ROOT / "frontend" / "src" / "App.tsx"


# --- Rollback safety ---------------------------------------------------------
# save() writes every field of the current schema. A config written by a newer
# build therefore carries keys an older build has never seen. load() runs at
# startup outside any try/except and the systemd unit is Restart=always with
# RestartSec=2, so an unfiltered TypeError here is a 2-second crash loop with
# no web UI left to fix it from — and the deploy won't repair it, because the
# playbook templates shaker.toml with force:false and rsyncs with --exclude=config/.


def test_unknown_audio_key_is_ignored_not_fatal() -> None:
    cfg = cfg_mod._from_dict({"audio": {"master_gain": 0.4, "some_future_field": 1.0}})
    assert cfg.audio.master_gain == 0.4
    assert cfg.audio.vibration_gain == AudioConfig().vibration_gain


def test_unknown_keys_ignored_in_every_section() -> None:
    cfg = cfg_mod._from_dict({
        "gt7": {"heartbeat_interval_s": 9.0, "future_gt7": "x"},
        "web": {"port": 8080, "future_web": 1},
        "audio": {"future_audio": True},
    })
    assert cfg.gt7.heartbeat_interval_s == 9.0
    assert cfg.web.port == 8080
    assert cfg.audio == AudioConfig()


def test_missing_keys_fall_back_to_defaults() -> None:
    cfg = cfg_mod._from_dict({"audio": {"device": "hw:1"}})
    assert cfg.audio.device == "hw:1"
    assert cfg.audio == AudioConfig(device="hw:1")


def test_empty_config_is_all_defaults() -> None:
    assert cfg_mod._from_dict({}) == Config()


def test_forward_written_config_reloads(tmp_path: Path) -> None:
    """The real rollback path: new build writes, old build reads."""
    path = tmp_path / "shaker.toml"
    cfg_mod.save(Config(audio=AudioConfig(master_gain=0.42)), path)
    path.write_text(path.read_text() + 'a_field_from_the_future = "boom"\n')
    assert cfg_mod.load(path).audio.master_gain == 0.42


# --- Round-trip --------------------------------------------------------------


def test_save_load_round_trips(tmp_path: Path) -> None:
    original = Config(
        gt7=GT7Config(ps5_ip="192.168.1.50", heartbeat_interval_s=3.0),
        web=WebConfig(host="0.0.0.0", port=80),
        audio=AudioConfig(master_gain=0.75, engine_rumble_max_hz=88.0),
    )
    path = tmp_path / "shaker.toml"
    cfg_mod.save(original, path)
    assert cfg_mod.load(path) == original


def test_empty_ps5_ip_round_trips_as_none(tmp_path: Path) -> None:
    path = tmp_path / "shaker.toml"
    cfg_mod.save(Config(gt7=GT7Config(ps5_ip=None)), path)
    assert cfg_mod.load(path).gt7.ps5_ip is None


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert cfg_mod.load(tmp_path / "nope.toml") == Config()


# --- Schema mirrors ----------------------------------------------------------
# AudioConfig is hand-mirrored into TypeScript and into the shipped TOML.
# Nothing else catches drift, and a missed field fails silently at runtime.


def test_shipped_toml_matches_dataclass_defaults() -> None:
    shipped = cfg_mod.load(_REPO_ROOT / "config" / "shaker.toml")
    assert shipped.audio == AudioConfig()


def test_typescript_audio_interface_matches_dataclass() -> None:
    body = re.search(
        r"export interface AudioConfig \{(.*?)\n\}", _FRONTEND_TYPES.read_text(), re.S
    )
    assert body, "could not find the AudioConfig interface"
    ts_keys = set(re.findall(r"^\s*(\w+)\??:", body.group(1), re.M))
    assert ts_keys == {f.name for f in fields(AudioConfig)}


def test_every_audio_field_has_a_ui_control() -> None:
    """Each AudioConfig field should be reachable from the tuning UI."""
    wired = set(re.findall(r'a\("(\w+)"\)', _APP_TSX.read_text()))
    assert {f.name for f in fields(AudioConfig)} - wired == set()


def test_restart_required_paths_resolve() -> None:
    for path in RESTART_REQUIRED_FIELDS:
        section, _, field_name = path.partition(".")
        assert hasattr(Config(), section), path
        assert field_name in {f.name for f in fields(getattr(Config(), section))}, path


# --- diff / merge ------------------------------------------------------------


def test_diff_paths_reports_dotted_changes() -> None:
    old = Config()
    new = cfg_mod.merge(old, {"audio": {"master_gain": 0.9}})
    assert cfg_mod.diff_paths(old, new) == {"audio.master_gain"}


def test_needs_restart_only_for_restart_fields() -> None:
    base = Config()
    assert not cfg_mod.needs_restart(base, cfg_mod.merge(base, {"audio": {"master_gain": 0.9}}))
    assert cfg_mod.needs_restart(base, cfg_mod.merge(base, {"audio": {"sample_rate": 44100}}))


def test_merge_leaves_other_sections_untouched() -> None:
    base = Config(gt7=GT7Config(ps5_ip="10.0.0.5"))
    merged = cfg_mod.merge(base, {"audio": {"master_gain": 0.1}})
    assert merged.gt7 == base.gt7
    assert merged.web == base.web
    assert merged.audio.master_gain == pytest.approx(0.1)
