from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from shaker import config as cfg_mod
from shaker import profiles as profiles_mod
from shaker.audio.bus import AudioBus, TelemetryFeatures
from shaker.config import Config
from shaker.gt7 import drivetrain
from shaker.gt7.client import GT7Client
from shaker.gt7.protocol import TelemetryPacket
from shaker.profiles import DEFAULT_PROFILE_NAME
from shaker.recording import SessionRecorder, list_sessions

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    get_config: Callable[[], Config],
    save_config: Callable[[Config], None],
    gt7: GT7Client,
    bus: AudioBus,
    recorder: SessionRecorder | None = None,
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
            source_audio = profiles_mod.get_audio(state, source, get_config().audio)
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
        audio = profiles_mod.get_audio(state, name, get_config().audio)
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
            "car": _car_identity(bus.car_code),
            "motion": _motion_diagnostics(bus.features),
            "muted": bus.muted,
            "axle": _axle_diagnostics(bus.features, gt7.latest_packet),
            "limiter": _limiter_diagnostics(bus),
            "recording": recorder.status() if recorder else None,
        }

    @app.get("/api/recordings")
    def read_recordings() -> dict[str, Any]:
        return {
            "status": recorder.status() if recorder else None,
            "sessions": list_sessions(),
        }

    @app.post("/api/recordings/start")
    def start_recording(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Begin capturing every packet to a file.

        Deliberately not a config field: recording is something you do, not
        something the rig is set to, and a flag persisted across restarts would
        eventually fill the SD card by being forgotten.
        """
        if recorder is None:
            raise HTTPException(status_code=503, detail="recorder unavailable")
        name = str((body or {}).get("name", "")).strip() or None
        recorder.start(name=name)
        return recorder.status()

    @app.post("/api/recordings/stop")
    def stop_recording() -> dict[str, Any]:
        if recorder is None:
            raise HTTPException(status_code=503, detail="recorder unavailable")
        return recorder.stop()

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

    @app.post("/api/test/wiring")
    def test_wiring() -> dict[str, Any]:
        """Pulse front, pause, pulse rear — bypassing the whole mix.

        The one question no amount of software can answer for itself: are the
        amp channels the way round the app assumes? A reversed pair inverts
        every routing decision and reads as "this feels subtly wrong" rather
        than as a bug, so it needs a deliberate check.
        """
        bus.trigger_wiring_check(pulse_s=1.0, gap_s=0.5)
        return {"ok": True, "pulse_s": 1.0, "gap_s": 0.5, "total_s": 2.5}

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


def _motion_diagnostics(f: TelemetryFeatures) -> dict[str, Any]:
    """Body-motion fields, next to independently derived references.

    All three are body-frame accelerations in m/s^2 with gravity excluded,
    identified on a live console by comparison against these same references
    (see protocol.py). The references stay published because they remain a
    useful cross-check: if a GT7 update moved the offsets, surge would stop
    tracking long_accel and it would be visible here rather than silent.
    """
    return {
        "has_motion": f.has_motion,
        "sway": f.sway,
        "heave": f.heave,
        "surge": f.surge,
        "long_accel": f.long_accel,
        "lat_accel": f.lat_accel,
    }


def _limiter_diagnostics(bus: AudioBus) -> dict[str, Any]:
    """How hard the output limiter is working, in dB of gain reduction.

    The bus carries linear gains because that is what the audio path applies;
    dB is what the number means to a person, so the conversion happens here
    rather than in the browser.

    This exists because the limiter's failure mode is silent. Driven too hard
    it stops being a safety net and becomes a compressor, and the symptom is
    not distortion but sameness — kerbs, shifts and road texture all arriving
    at one level. That reads as "the rig feels flat", which is exactly the
    complaint most likely to be answered by turning the gain up again.
    """
    def db(gain: float) -> float:
        # Floor the input rather than the output: log10(0) is not an error we
        # want reaching the UI as an Infinity that JSON cannot encode.
        #
        # max() on the way out is not redundant with it. The limiter never
        # applies gain above unity, so reduction is never negative — but
        # -20*log10(1.0) is -0.0, which survives rounding and JSON and renders
        # as "-0.0 dB" on an idle rig.
        return max(0.0, -20.0 * math.log10(max(gain, 1e-6)))

    now = db(bus.limit_gain)
    # The hold is always at least as deep as the current reduction by
    # construction, but the two fields are written on consecutive lines of the
    # audio callback and read here without a lock. On a fast ramp this can
    # sample one before and one after an update and report a peak shallower
    # than the present value — harmless as a measurement, but it renders as an
    # obviously broken meter. Clamping costs nothing and cannot mask a real
    # fault, since the invariant is one-directional.
    peak = max(now, db(bus.limit_hold))

    return {
        "reduction_db": round(now, 2),
        "peak_reduction_db": round(peak, 2),
        "duty_pct": round(100.0 * bus.limit_duty, 1),
    }


def _car_identity(car_code: int | None) -> dict[str, Any]:
    """What the drivetrain table knows about the car currently being driven.

    Surfaced so a driver can confirm the lookup actually resolved — an
    unrecognised car silently falls back to the configured biases, which is
    correct but indistinguishable from the routing working.
    """
    return {
        "code": car_code,
        "layout": drivetrain.layout_for(car_code),
        "driven_axle": drivetrain.driven_axle(car_code),
        "engine_position": drivetrain.engine_position(car_code),
    }


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


def _axle_diagnostics(
    f: TelemetryFeatures, p: TelemetryPacket | None
) -> dict[str, Any]:
    """Front/rear derived values plus the raw corner fields they came from.

    This block exists because the reductions cannot show you what they threw
    away. Reading it on a live rig settled two protocol questions in one
    session (2026-08-05): wheel_rps is rad/s but sent negated, and the
    FL/FR/RL/RR corner order does match the offsets — proved by a first-gear
    launch, where only the driven axle outran the car.

    Still unverified: whether speed_mps is signed in reverse. If it is an
    unsigned magnitude, reversing reads as total lockup on all four corners.

    So it keeps shipping the *raw* per-corner numbers next to the derived ones.
    A permuted mapping or a units error is obvious when the four wheels can be
    read individually against vehicle speed, and invisible in a max() of
    absolute values.

    The legacy scalars ride along so drift between the old whole-car values and
    the new per-axle ones stays visible in one place.
    """
    raw: dict[str, Any] | None = None
    if p is not None:
        raw = {
            # The reference the wheel speeds are compared against. Watch this
            # against wheel_surface_speed_* at a steady cruise.
            "speed_mps": p.speed_mps,
            "current_gear": p.current_gear,
            "wheel_rps_FL": p.wheel_rps_FL,
            "wheel_rps_FR": p.wheel_rps_FR,
            "wheel_rps_RL": p.wheel_rps_RL,
            "wheel_rps_RR": p.wheel_rps_RR,
            "tire_radius_FL": p.tire_radius_FL,
            "tire_radius_FR": p.tire_radius_FR,
            "tire_radius_RL": p.tire_radius_RL,
            "tire_radius_RR": p.tire_radius_RR,
            # rps * radius, after the parse-time sign normalization. Should
            # equal speed_mps at a steady cruise; that it read -speed_mps is
            # how the inverted sign was caught.
            "wheel_surface_speed_FL": p.wheel_rps_FL * p.tire_radius_FL,
            "wheel_surface_speed_FR": p.wheel_rps_FR * p.tire_radius_FR,
            "wheel_surface_speed_RL": p.wheel_rps_RL * p.tire_radius_RL,
            "wheel_surface_speed_RR": p.wheel_rps_RR * p.tire_radius_RR,
            "suspension_FL": p.suspension_FL,
            "suspension_FR": p.suspension_FR,
            "suspension_RL": p.suspension_RL,
            "suspension_RR": p.suspension_RR,
        }
    return {
        "slip_front": f.slip_front,
        "slip_rear": f.slip_rear,
        "suspension_activity_front": f.suspension_activity_front,
        "suspension_activity_rear": f.suspension_activity_rear,
        # Legacy whole-car scalars — the ones the audio path actually reads.
        "slip_magnitude": f.slip_magnitude,
        "suspension_activity": f.suspension_activity,
        "raw": raw,
    }
