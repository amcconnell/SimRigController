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
    # 1 = one mono channel, exactly as before two-channel support existed.
    # 2 = channel 0 front, channel 1 rear. Opt-in rather than auto-probed:
    # ALSA's "default" device happily accepts two channels whether or not
    # anything is wired to the second one, so probing always "succeeds" and
    # would silently send every rear-routed effect to a dead conductor.
    output_channels: int = 1
    # Corrects for the two shakers not being equally coupled to the driver —
    # a seat transmits far more of what it is given than a pedal deck does.
    # Hardware compensation, kept separate from the per-effect biases so those
    # stay about placement rather than absorbing a rig quirk.
    rear_gain_trim: float = 1.0
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
    # Clamp the derived frequency into the band a shaker can actually render.
    # rpm/60 is engine order 1, so it leaves the useful band on every redline
    # pull (7000 rpm = 117 Hz, a 13k-rpm car = 217 Hz) where it radiates as
    # airborne buzz through the frame instead of felt motion.
    engine_rumble_min_hz: float = 26.0
    engine_rumble_max_hz: float = 100.0
    # Engine and drivetrain thrum reaches a driver through the floor and seat
    # rather than the pedals. Mild rear bias by default; the engine's real
    # position is a per-car fact this app cannot yet read, so this is taste.
    engine_rumble_bias: float = 0.3
    # Brake rumble: low-frequency hum while braking; amplitude scales with brake above threshold.
    brake_rumble_enabled: bool = True
    brake_rumble_gain: float = 1.0
    brake_rumble_freq_hz: float = 30.0
    brake_rumble_threshold_pct: float = 20.0
    # Strongly front. Not a guess about weight transfer — pad judder, ABS
    # pulsing and tyre scrub reach a real driver through the brake pedal, so
    # with a pedal-deck shaker this reproduces the actual physical channel.
    brake_rumble_bias: float = -0.7
    # Rev limiter: distinct buzz when engine_rpm / max_alert_rpm crosses trigger_pct.
    rev_limiter_enabled: bool = True
    rev_limiter_gain: float = 1.0
    rev_limiter_freq_hz: float = 75.0
    rev_limiter_trigger_pct: float = 95.0
    # Centred, and it should stay there: rpm/max_alert_rpm is a scalar ratio
    # with no spatial content at all. The control exists for taste, not physics.
    rev_limiter_bias: float = 0.0
    # Wheel slip: buzz when any wheel speed diverges from vehicle speed (spin or lockup).
    wheel_slip_enabled: bool = True
    wheel_slip_gain: float = 1.0
    wheel_slip_freq_hz: float = 90.0
    # Slip is judged as a *ratio* of vehicle speed, because that is what a tyre
    # responds to — peak grip sits near 10-20% slip regardless of how fast the
    # car is going. A fixed m/s threshold cannot express that: 2 m/s is 20% of
    # a 36 km/h hairpin (already sliding, no warning) and 2.5% at 288 km/h
    # (inside normal grip, so it chatters on every straight). Same tyre
    # behaviour, opposite outcomes, and no single value fixes both ends.
    wheel_slip_threshold_pct: float = 8.0
    # Ratio above the threshold at which the effect reaches full amplitude.
    wheel_slip_scale_pct: float = 12.0
    # Absolute floor, applied as well as the ratio. Near a standstill the ratio
    # denominator collapses and any wheel twitch reads as enormous slip, so a
    # small m/s minimum keeps a parked car and a slow pit lane quiet.
    wheel_slip_threshold_mps: float = 0.5
    # Lockup gets its own, lower voice. A locked tyre grinds; a spinning one
    # scrabbles higher. Both clear the 30 Hz brake, 44 Hz gear-shift and 75 Hz
    # limiter bands so a corner entry stays legible as separate events.
    wheel_slip_lock_freq_hz: float = 65.0
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
    # Rear by default: driveline shock reacts through the driven axle, which is
    # the rear on the majority of cars, and wrong for front-wheel drive.
    gear_shift_bias: float = 0.5
    # Let the car database decide *which end* the gear-shift and engine effects
    # belong on, using the car code in the telemetry. The bias values above
    # then set only how strongly, not where. An unknown car — or this switched
    # off — falls back to the configured values exactly as written.
    drivetrain_routing_enabled: bool = True


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
    # Channel count is fixed when the PortAudio stream is opened, exactly like
    # sample rate and buffer size. Hot-swapping it would have the callback
    # index a second column the open stream does not have.
    "audio.output_channels",
})


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        log.info("config %s missing, using defaults", path)
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return _from_dict(raw)


def _known(section: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the dataclass doesn't declare, warning about each one.

    `save()` writes every field of the current schema, so a config written by
    a newer build carries fields an older one has never heard of. Without this
    filter `Section(**raw)` raises TypeError, `load()` at startup isn't guarded
    (runtime.run), and the systemd unit is Restart=always/RestartSec=2 — so a
    rollback becomes a 2-second crash loop with no web UI to fix it from.
    Unknown keys fall back to defaults instead. profiles.get_audio already
    filters for the same reason.
    """
    fields_ = {f.name for f in fields(section)}
    unknown = set(raw) - fields_
    if unknown:
        log.warning(
            "ignoring unknown %s config keys (newer schema?): %s",
            section.__name__, ", ".join(sorted(unknown)),
        )
    return {k: v for k, v in raw.items() if k in fields_}


def _from_dict(raw: dict[str, Any]) -> Config:
    gt7_raw = dict(raw.get("gt7", {}))
    if gt7_raw.get("ps5_ip") == "":
        gt7_raw["ps5_ip"] = None
    return Config(
        gt7=GT7Config(**_known(GT7Config, gt7_raw)),
        web=WebConfig(**_known(WebConfig, raw.get("web", {}))),
        audio=AudioConfig(**_known(AudioConfig, raw.get("audio", {}))),
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
