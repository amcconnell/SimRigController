#!/usr/bin/env python3
"""Render a recorded session through the DSP offline, and compare settings.

Recording a lap only pays off if the lap can be rendered twice. This is the
other half: same input, two parameter sets, measured rather than remembered.

    # What the current config does with this lap
    python scripts/replay.py sessions/session-20260807-1830.jsonl

    # Does doubling master gain help, or just flatten it?
    python scripts/replay.py <file> --set master_gain=1.0

    # Compare two saved profiles
    python scripts/replay.py <file> --profile "Gr.3" --against "road car"

    # Write the render out to look at or listen to
    python scripts/replay.py <file> --wav /tmp/lap.wav

Watch **crest**, not peak. The limiter pins peak to the ceiling by
construction, so two settings that feel completely different report the same
peak; crest is what collapses when a mix is being compressed rather than driven.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

# Allow running from a checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shaker import config as cfg_mod                      # noqa: E402
from shaker import profiles as profiles_mod               # noqa: E402
from shaker.config import AudioConfig                     # noqa: E402
from shaker.recording import read_session                 # noqa: E402
from shaker.replay import compare, replay, write_wav      # noqa: E402


def _apply_overrides(cfg: AudioConfig, pairs: list[str]) -> AudioConfig:
    """`--set master_gain=1.0` — typed against the dataclass, not guessed."""
    fields = {f.name: f for f in AudioConfig.__dataclass_fields__.values()}
    updates = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if key not in fields:
            raise SystemExit(f"unknown audio field: {key!r}")
        current = getattr(cfg, key)
        try:
            if isinstance(current, bool):
                updates[key] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                updates[key] = int(raw)
            elif isinstance(current, float):
                updates[key] = float(raw)
            else:
                updates[key] = raw
        except ValueError as exc:
            raise SystemExit(f"bad value for {key}: {raw!r} ({exc})") from exc
    return replace(cfg, **updates)


def _resolve(name: str | None, live: AudioConfig) -> AudioConfig:
    if name is None:
        return live
    state = profiles_mod.load_state()
    try:
        return profiles_mod.get_audio(state, name, live)
    except KeyError:
        known = ", ".join(profiles_mod.list_names(state))
        raise SystemExit(f"unknown profile {name!r}. known: {known}") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", type=Path, help="a .jsonl recording")
    ap.add_argument("--profile", help="render using this saved profile")
    ap.add_argument("--against", help="second profile, to compare against --profile")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override an audio field (repeatable)")
    ap.add_argument("--wav", type=Path, help="write the rendered audio here")
    args = ap.parse_args(argv)

    if not args.session.exists():
        raise SystemExit(f"no such file: {args.session}")

    header, packets = read_session(args.session)
    pkts = list(packets)
    if not pkts:
        raise SystemExit(f"{args.session} contains no packets")
    print(f"{args.session.name}: {len(pkts)} packets, recorded {header.get('started_at','?')}\n")

    live = cfg_mod.load().audio
    base = _apply_overrides(_resolve(args.profile, live), args.set)

    if args.against:
        other = _apply_overrides(_resolve(args.against, live), args.set)
        print(compare(pkts, base, other,
                      label_a=args.profile or "current config",
                      label_b=args.against).summary())
        return 0

    result = replay(pkts, base, keep_audio=args.wav is not None)
    print(result.summary())

    if args.wav is not None and result.audio is not None:
        write_wav(args.wav, result.audio, base.sample_rate)
        print(f"\nwrote {args.wav}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
