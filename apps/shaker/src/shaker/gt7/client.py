from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from shaker.config import GT7Config
from shaker.gt7.protocol import (
    GT7_BIND_PORT,
    GT7_RECEIVE_PORT,
    HEARTBEAT,
    TelemetryPacket,
    decrypt_packet,
    parse_packet,
)

PacketCallback = Callable[[TelemetryPacket], None]

log = logging.getLogger(__name__)

_DISCOVERY_HEARTBEAT_S = 1.0
_STALE_AFTER_S = 2.0


@dataclass
class Status:
    state: str  # "starting" | "discovering" | "connected" | "stale"
    ps5_ip: str | None
    packet_count: int
    packets_per_sec: float
    last_packet_age_s: float | None
    discovery_elapsed_s: float


class GT7Client:
    """Asyncio UDP client: discovers PS5, sends heartbeats, parses telemetry."""

    def __init__(self, config: GT7Config, on_packet: PacketCallback | None = None) -> None:
        self._config = config
        self._configured_ip: str | None = config.ps5_ip
        self._discovered_ip: str | None = None
        self._on_packet = on_packet
        self.latest_packet: TelemetryPacket | None = None
        self._packet_count = 0
        self._packet_window: list[float] = []  # timestamps for rate calc
        self._last_packet_mono: float | None = None
        self._started_mono: float = 0.0
        self._transport: asyncio.DatagramTransport | None = None
        self._stop = asyncio.Event()

    @property
    def ps5_ip(self) -> str | None:
        return self._configured_ip or self._discovered_ip

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._started_mono = time.monotonic()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _GT7Protocol(self),
            local_addr=("0.0.0.0", GT7_BIND_PORT),
            allow_broadcast=True,
        )
        log.info("listening on :%d for GT7 telemetry", GT7_BIND_PORT)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="gt7-heartbeat")
        try:
            await self._stop.wait()
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            self._transport.close()
            self._transport = None

    def stop(self) -> None:
        self._stop.set()

    def update_config(self, config: GT7Config) -> None:
        """Apply hot-reloadable config changes (heartbeat interval, ps5_ip override)."""
        old_ip = self._configured_ip
        self._config = config
        self._configured_ip = config.ps5_ip
        if config.ps5_ip != old_ip:
            log.info("ps5_ip override changed: %r -> %r", old_ip, config.ps5_ip)
            # Force re-discovery if cleared, or jump to new target.
            if config.ps5_ip is None:
                self._discovered_ip = None

    def status(self) -> Status:
        now = time.monotonic()
        last_age = (now - self._last_packet_mono) if self._last_packet_mono else None
        if last_age is None:
            state = "discovering" if self.ps5_ip is None else "starting"
        elif last_age > _STALE_AFTER_S:
            state = "stale"
        else:
            state = "connected"

        # packets/sec over the last second
        cutoff = now - 1.0
        recent = [t for t in self._packet_window if t >= cutoff]
        self._packet_window = recent
        return Status(
            state=state,
            ps5_ip=self.ps5_ip,
            packet_count=self._packet_count,
            packets_per_sec=float(len(recent)),
            last_packet_age_s=last_age,
            discovery_elapsed_s=now - self._started_mono,
        )

    # Called from _GT7Protocol on every received UDP datagram.
    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            decrypted = decrypt_packet(data)
            packet = parse_packet(decrypted)
        except Exception:
            log.exception("decrypt/parse error from %s", addr)
            return

        if self._discovered_ip is None and self._configured_ip is None:
            self._discovered_ip = addr[0]
            log.info("PS5 discovered at %s", self._discovered_ip)

        self.latest_packet = packet
        self._packet_count += 1
        now = time.monotonic()
        self._last_packet_mono = now
        self._packet_window.append(now)

        if self._on_packet is not None:
            try:
                self._on_packet(packet)
            except Exception:
                log.exception("on_packet callback failed")

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            target = self.ps5_ip
            if self._transport is None:
                await asyncio.sleep(0.1)
                continue
            try:
                if target:
                    self._transport.sendto(HEARTBEAT, (target, GT7_RECEIVE_PORT))
                    delay = self._config.heartbeat_interval_s
                else:
                    self._transport.sendto(HEARTBEAT, ("255.255.255.255", GT7_RECEIVE_PORT))
                    delay = _DISCOVERY_HEARTBEAT_S
            except OSError as exc:
                log.warning("heartbeat send failed: %s", exc)
                delay = 1.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass


class _GT7Protocol(asyncio.DatagramProtocol):
    def __init__(self, client: GT7Client) -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._client._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP error: %s", exc)
