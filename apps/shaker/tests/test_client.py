"""GT7Client source pinning: only the locked console may drive the shaker."""

from __future__ import annotations

import pytest

from shaker.config import GT7Config
from shaker.gt7 import client as client_mod
from shaker.gt7.client import GT7Client
from shaker.gt7.protocol import TelemetryPacket

PS5 = "192.168.1.135"
ROGUE = "192.168.1.50"


@pytest.fixture
def passthrough_parse(monkeypatch):
    """Bypass decrypt/parse — datagram payload is irrelevant to routing."""
    monkeypatch.setattr(client_mod, "decrypt_packet", lambda data: data)
    monkeypatch.setattr(client_mod, "parse_packet", lambda data: TelemetryPacket())


def make_client(**kwargs) -> tuple[GT7Client, list]:
    received: list[TelemetryPacket] = []
    client = GT7Client(GT7Config(**kwargs), on_packet=received.append)
    return client, received


def test_first_sender_wins_discovery(passthrough_parse):
    client, received = make_client()
    client._on_datagram(b"x", (PS5, 33740))
    assert client.ps5_ip == PS5
    assert len(received) == 1


def test_rogue_source_ignored_after_discovery(passthrough_parse):
    client, received = make_client()
    client._on_datagram(b"x", (PS5, 33740))
    client._on_datagram(b"x", (ROGUE, 33740))
    client._on_datagram(b"x", (ROGUE, 33740))
    assert len(received) == 1
    assert client.ps5_ip == PS5


def test_configured_ip_pins_immediately(passthrough_parse):
    client, received = make_client(ps5_ip=PS5)
    client._on_datagram(b"x", (ROGUE, 33740))
    assert received == []
    client._on_datagram(b"x", (PS5, 33740))
    assert len(received) == 1


def test_rogue_packets_do_not_bump_liveness(passthrough_parse):
    client, _ = make_client(ps5_ip=PS5)
    client._on_datagram(b"x", (ROGUE, 33740))
    assert client._last_packet_mono is None
    assert client.status().state == "starting"


def test_config_change_clears_ignore_set(passthrough_parse):
    client, received = make_client()
    client._on_datagram(b"x", (PS5, 33740))
    client._on_datagram(b"x", (ROGUE, 33740))
    assert ROGUE in client._ignored_sources
    # Repointing at the previously-rogue source must not stay blocked.
    client.update_config(GT7Config(ps5_ip=ROGUE))
    client._on_datagram(b"x", (ROGUE, 33740))
    assert len(received) == 2


def test_stale_lock_rediscovers(passthrough_parse):
    client, received = make_client()
    client._on_datagram(b"x", (PS5, 33740))
    client._on_datagram(b"x", (ROGUE, 33740))
    assert len(received) == 1

    # Watchdog fires after prolonged silence — lock drops.
    client._check_stale(client._last_packet_mono + client_mod._REDISCOVER_AFTER_S + 1)
    assert client.ps5_ip is None

    # A different source may now claim the lock.
    client._on_datagram(b"x", (ROGUE, 33740))
    assert client.ps5_ip == ROGUE
    assert len(received) == 2


def test_brief_staleness_keeps_lock(passthrough_parse):
    stale_calls = []
    client = GT7Client(GT7Config(), on_stale=lambda: stale_calls.append(1))
    client._on_datagram(b"x", (PS5, 33740))

    client._check_stale(client._last_packet_mono + client_mod._STALE_AFTER_S + 1)
    assert stale_calls  # features reset…
    assert client.ps5_ip == PS5  # …but the lock survives a short dropout


def test_configured_ip_never_dropped_by_staleness(passthrough_parse):
    client, _ = make_client(ps5_ip=PS5)
    client._on_datagram(b"x", (PS5, 33740))
    client._check_stale(client._last_packet_mono + client_mod._REDISCOVER_AFTER_S + 1)
    assert client.ps5_ip == PS5
