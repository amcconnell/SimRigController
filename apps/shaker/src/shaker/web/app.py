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
from shaker.audio.bus import AudioBus
from shaker.config import Config
from shaker.gt7.client import GT7Client
from shaker.gt7.protocol import TelemetryPacket

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
            save_config(new_cfg)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return cfg_mod.to_dict(new_cfg)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "gt7": asdict(gt7.status()),
            "telemetry": _summarize_packet(gt7.latest_packet),
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
