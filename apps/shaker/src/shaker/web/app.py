from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from shaker import config as cfg_mod
from shaker import profiles as profiles_mod
from shaker.audio.bus import AudioBus
from shaker.config import Config
from shaker.gt7.client import GT7Client
from shaker.gt7.protocol import TelemetryPacket
from shaker.profiles import DEFAULT_PROFILE_NAME

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    get_config: Callable[[], Config],
    save_config: Callable[[Config], None],
    gt7: GT7Client,
    bus: AudioBus,
) -> FastAPI:
    app = FastAPI(title="SimRig Shaker")

    @app.get("/api/config")
    def read_config() -> dict[str, Any]:
        return cfg_mod.to_dict(get_config())

    @app.put("/api/config")
    def update_config(updates: dict[str, Any]) -> dict[str, Any]:
        try:
            current = get_config()
            new_cfg = cfg_mod.merge(current, updates)
            # If audio fields changed and a non-default profile is active, mirror
            # the new audio config back into the profile so it persists.
            if "audio" in updates:
                state = profiles_mod.load_state()
                if state.get("active") == DEFAULT_PROFILE_NAME:
                    raise HTTPException(
                        status_code=409,
                        detail="The default profile is read-only. Create a new profile to edit audio settings.",
                    )
                profiles_mod.update_active_audio(state, new_cfg.audio)
                profiles_mod.save_state(state)
            save_config(new_cfg)
        except HTTPException:
            raise
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return cfg_mod.to_dict(new_cfg)

    # --- Profiles ----------------------------------------------------------

    @app.get("/api/profiles")
    def list_profiles() -> dict[str, Any]:
        state = profiles_mod.load_state()
        return {"active": state.get("active", DEFAULT_PROFILE_NAME), "names": profiles_mod.list_names(state)}

    @app.post("/api/profiles")
    def create_profile(body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        source = str(body.get("source", DEFAULT_PROFILE_NAME))
        if not name:
            raise HTTPException(status_code=400, detail="profile name required")
        state = profiles_mod.load_state()
        try:
            source_audio = profiles_mod.get_audio(state, source)
            profiles_mod.create(state, name, source_audio)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        profiles_mod.save_state(state)
        return {"active": state["active"], "names": profiles_mod.list_names(state)}

    @app.delete("/api/profiles/{name}")
    def delete_profile(name: str) -> dict[str, Any]:
        state = profiles_mod.load_state()
        try:
            profiles_mod.delete(state, name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        profiles_mod.save_state(state)
        # If we just deleted the active profile, the state already fell back
        # to default; reflect that in the live audio config too.
        if state["active"] == DEFAULT_PROFILE_NAME:
            _activate(state, DEFAULT_PROFILE_NAME)
        return {"active": state["active"], "names": profiles_mod.list_names(state)}

    @app.post("/api/profiles/{name}/rename")
    def rename_profile(name: str, body: dict[str, Any]) -> dict[str, Any]:
        new_name = str(body.get("new_name", "")).strip()
        state = profiles_mod.load_state()
        try:
            profiles_mod.rename(state, name, new_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        profiles_mod.save_state(state)
        return {"active": state["active"], "names": profiles_mod.list_names(state)}

    @app.post("/api/profiles/{name}/activate")
    def activate_profile(name: str) -> dict[str, Any]:
        state = profiles_mod.load_state()
        try:
            _activate(state, name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"active": state["active"], "names": profiles_mod.list_names(state)}

    def _activate(state: dict[str, Any], name: str) -> None:
        audio = profiles_mod.get_audio(state, name)
        new_live = profiles_mod.apply_to_live_config(audio, get_config())
        save_config(new_live)  # triggers watcher reload
        state["active"] = name
        profiles_mod.save_state(state)

    # --- Mute --------------------------------------------------------------

    @app.get("/api/mute")
    def read_mute() -> dict[str, Any]:
        return {"muted": bus.muted}

    @app.post("/api/mute")
    def set_mute(body: dict[str, Any]) -> dict[str, Any]:
        muted = bool(body.get("muted", not bus.muted))
        bus.muted = muted
        return {"muted": bus.muted}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "gt7": asdict(gt7.status()),
            "telemetry": _summarize_packet(gt7.latest_packet),
            "muted": bus.muted,
        }

    @app.post("/api/test/vibration")
    def test_vibration() -> dict[str, Any]:
        bus.trigger_test_vibration(duration_s=1.0)
        return {"ok": True, "duration_s": 1.0}

    @app.post("/api/test/gear_shift")
    def test_gear_shift() -> dict[str, Any]:
        bus.trigger_test_gear_shift()
        return {"ok": True}

    @app.post("/api/test/engine_sweep")
    def test_engine_sweep() -> dict[str, Any]:
        bus.trigger_test_engine_sweep(duration_s=3.0, peak_rpm=7000.0)
        return {"ok": True, "duration_s": 3.0, "peak_rpm": 7000.0}

    @app.post("/api/test/brake_rumble")
    def test_brake_rumble() -> dict[str, Any]:
        bus.trigger_test_brake_rumble(duration_s=2.0, peak_brake=220)
        return {"ok": True, "duration_s": 2.0}

    @app.post("/api/test/rev_limiter")
    def test_rev_limiter() -> dict[str, Any]:
        bus.trigger_test_rev_limiter(duration_s=2.0)
        return {"ok": True, "duration_s": 2.0}

    @app.post("/api/test/wheel_slip")
    def test_wheel_slip() -> dict[str, Any]:
        bus.trigger_test_wheel_slip(duration_s=2.0, peak_slip_mps=7.0)
        return {"ok": True, "duration_s": 2.0}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    # The Vite build emits hashed bundles into static/assets/. Mount it so the
    # generated <script> / <link> tags resolve. (Keeping /static mounted too
    # for any ad-hoc static files dropped in alongside.)
    _ASSETS_DIR = _STATIC_DIR / "assets"
    if _ASSETS_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app


def _summarize_packet(p: TelemetryPacket | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "engine_rpm": p.engine_rpm,
        "speed_kph": p.speed_mps * 3.6,
        "throttle": p.throttle,
        "brake": p.brake,
        "current_gear": p.current_gear,
        "lap_count": p.lap_count,
        "packet_id": p.packet_id,
    }
