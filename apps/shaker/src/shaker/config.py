from __future__ import annotations

import logging
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "shaker.toml"


@dataclass(frozen=True)
class GT7Config:
    ps5_ip: str | None = None
    heartbeat_interval_s: float = 5.0
    discovery_timeout_s: float = 30.0


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class AudioConfig:
    device: str = "default"
    sample_rate: int = 48000
    buffer_ms: int = 20
    master_gain: float = 0.6
    vibration_enabled: bool = True
    vibration_gain: float = 1.0
    # SimHub-style response filter (input gain → threshold → min force → gamma).
    vibration_input_gain_pct: float = 100.0
    vibration_threshold_pct: float = 0.0
    vibration_min_force_pct: float = 0.0
    vibration_gamma: float = 1.0
    # Speed-driven blend into a higher noise band — preserves the low-band feel
    # at low speed and adds higher-frequency content at pace.
    vibration_speed_blend_low_mps: float = 20.0
    vibration_speed_blend_high_mps: float = 50.0
    # Engine rumble: continuous sine derived from RPM, amplitude from throttle.
    # rpm_divisor maps RPM to Hz (e.g., 60 → 100 Hz at 6000 RPM).
    engine_rumble_enabled: bool = True
    engine_rumble_gain: float = 1.0
    engine_rumble_rpm_divisor: float = 60.0
    gear_shift_enabled: bool = True
    gear_shift_gain: float = 1.0
    gear_shift_freq_hz: float = 44.0
    gear_shift_duration_ms: int = 80
    # RPM-driven modulation: gain factor is flat at min below the low %,
    # ramps linearly up to max at the high %, flat at max above.
    gear_shift_rpm_pct_low: float = 50.0
    gear_shift_rpm_pct_high: float = 90.0
    gear_shift_min_gain_pct: float = 50.0
    gear_shift_max_gain_pct: float = 100.0


@dataclass(frozen=True)
class Config:
    gt7: GT7Config = field(default_factory=GT7Config)
    web: WebConfig = field(default_factory=WebConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)


# Fields whose change requires a full process restart (vs. hot-reload).
# Dotted paths from the Config root.
RESTART_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "web.host",
    "web.port",
    "audio.device",
    "audio.sample_rate",
    "audio.buffer_ms",
})


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        log.info("config %s missing, using defaults", path)
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return _from_dict(raw)


def _from_dict(raw: dict[str, Any]) -> Config:
    gt7_raw = dict(raw.get("gt7", {}))
    if gt7_raw.get("ps5_ip") == "":
        gt7_raw["ps5_ip"] = None
    return Config(
        gt7=GT7Config(**gt7_raw),
        web=WebConfig(**raw.get("web", {})),
        audio=AudioConfig(**raw.get("audio", {})),
    )


def to_dict(cfg: Config) -> dict[str, Any]:
    return {
        "gt7": asdict(cfg.gt7),
        "web": asdict(cfg.web),
        "audio": asdict(cfg.audio),
    }


def save(cfg: Config, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for section_name, section_dict in to_dict(cfg).items():
        lines.append(f"[{section_name}]")
        for key, value in section_dict.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)


def _toml_value(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f'"{value}"'


def diff_paths(old: Config, new: Config) -> set[str]:
    """Return the set of dotted field paths that differ between old and new."""
    changed: set[str] = set()
    for section in fields(Config):
        old_section = getattr(old, section.name)
        new_section = getattr(new, section.name)
        for f_ in fields(old_section):
            if getattr(old_section, f_.name) != getattr(new_section, f_.name):
                changed.add(f"{section.name}.{f_.name}")
    return changed


def needs_restart(old: Config, new: Config) -> bool:
    return bool(diff_paths(old, new) & RESTART_REQUIRED_FIELDS)


def merge(cfg: Config, updates: dict[str, Any]) -> Config:
    """Apply a nested dict of updates to a Config, returning a new Config."""
    sections: dict[str, Any] = {}
    for section in fields(Config):
        current = getattr(cfg, section.name)
        section_updates = updates.get(section.name, {})
        if section_updates:
            sections[section.name] = replace(current, **section_updates)
        else:
            sections[section.name] = current
    return Config(**sections)
