from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from pathlib import Path

import uvicorn
import watchfiles

from shaker import config as cfg_mod
from shaker.audio.bus import AudioBus
from shaker.audio.stream import AudioOutput
from shaker.config import Config
from shaker.gt7.client import GT7Client
from shaker.web.app import create_app

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RESTART = 75


@dataclass
class _State:
    config: Config


async def run(config_path: Path = cfg_mod.DEFAULT_CONFIG_PATH) -> int:
    """Run the app until shutdown or restart-required config change.

    Returns EXIT_RESTART if the config changed in a way that needs a restart,
    EXIT_OK if shut down cleanly via signal.
    """
    state = _State(config=cfg_mod.load(config_path))
    log.info("loaded config from %s", config_path)

    bus = AudioBus(state.config.audio)
    audio = AudioOutput(bus)
    gt7 = GT7Client(state.config.gt7, on_packet=bus.push_packet)

    def get_config() -> Config:
        return state.config

    def save_config(new: Config) -> None:
        cfg_mod.save(new, config_path)

    fastapi_app = create_app(get_config=get_config, save_config=save_config, gt7=gt7, bus=bus)
    ucfg = uvicorn.Config(
        fastapi_app,
        host=state.config.web.host,
        port=state.config.web.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(ucfg)

    stop = asyncio.Event()
    restart = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    workers = [
        asyncio.create_task(gt7.run(), name="gt7"),
        asyncio.create_task(audio.run(), name="audio"),
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(_watch_config(config_path, state, gt7, bus, restart), name="watcher"),
    ]
    triggers = [
        asyncio.create_task(stop.wait(), name="stop"),
        asyncio.create_task(restart.wait(), name="restart"),
    ]

    log.info("web ui: http://%s:%d", state.config.web.host, state.config.web.port)

    try:
        await asyncio.wait(workers + triggers, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in triggers:
            if not t.done():
                t.cancel()
        gt7.stop()
        audio.stop()
        server.should_exit = True

        # Give workers a brief grace period to shut down naturally. Uvicorn's
        # graceful shutdown will hang if a long-poll client (e.g. the browser
        # status poller) keeps requests in-flight, so we escalate to force_exit
        # and cancellation if they don't exit on their own.
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers, *triggers, return_exceptions=True),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            log.warning("graceful shutdown timed out; forcing")
            server.force_exit = True
            for t in workers:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*workers, *triggers, return_exceptions=True)

    return EXIT_RESTART if restart.is_set() else EXIT_OK


async def _watch_config(
    path: Path,
    state: _State,
    gt7: GT7Client,
    bus: AudioBus,
    restart: asyncio.Event,
) -> None:
    async for _changes in watchfiles.awatch(path, stop_event=None):
        try:
            new_config = cfg_mod.load(path)
        except Exception:
            log.exception("failed to reload config")
            continue

        old_config = state.config
        changed = cfg_mod.diff_paths(old_config, new_config)
        if not changed:
            continue

        log.info("config changed: %s", sorted(changed))
        if cfg_mod.needs_restart(old_config, new_config):
            log.info("change requires restart")
            state.config = new_config
            restart.set()
            return

        state.config = new_config
        sections = {c.split(".")[0] for c in changed}
        if "gt7" in sections:
            gt7.update_config(new_config.gt7)
        if "audio" in sections:
            bus.update_audio_config(new_config.audio)
