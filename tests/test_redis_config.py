"""Unit tests for the Redis engine adapter's config generation.

No live Domino, no redis-server — these run anywhere with plain pytest and
guard the "big pipes + don't lose data" behavior the bank cares about:

  - noeviction (a database must never silently drop stored keys)
  - maxmemory sized to the hardware tier, not a flat 1 GB
  - io-threads / tcp tuning present
  - the password lands in the conf and the file is 0600

Run: python3 -m pytest tests/test_redis_config.py -q
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbapp.engines import _common  # noqa: E402
from dbapp.engines import redis as redis_engine  # noqa: E402
from dbapp.engines.redis import RedisAdapter  # noqa: E402


@pytest.fixture
def adapter() -> RedisAdapter:
    return RedisAdapter()


# ---------------------------------------------------------------------------
# maxmemory sizing — the "big pipe"
# ---------------------------------------------------------------------------
def test_maxmemory_explicit_override_wins(adapter):
    assert adapter._compute_maxmemory({"redis_maxmemory": "8gb"}) == "8gb"


def test_maxmemory_scales_with_container(adapter, monkeypatch):
    # 10 GiB container → 60% default → 6144 mb
    monkeypatch.setattr(_common, "container_memory_bytes", lambda: 10 * 1024**3)
    assert adapter._compute_maxmemory({}) == "6144mb"


def test_maxmemory_custom_fraction(adapter, monkeypatch):
    monkeypatch.setattr(_common, "container_memory_bytes", lambda: 10 * 1024**3)
    # 50% of 10 GiB = 5120 mb
    assert adapter._compute_maxmemory({"redis_maxmemory_fraction": 0.5}) == "5120mb"


def test_maxmemory_floor_for_tiny_tier(adapter, monkeypatch):
    # 128 MiB container * 0.6 = 76 mb, but we floor at 256 mb.
    monkeypatch.setattr(_common, "container_memory_bytes", lambda: 128 * 1024**2)
    assert adapter._compute_maxmemory({}) == "256mb"


def test_maxmemory_fallback_when_undetectable(adapter, monkeypatch):
    monkeypatch.setattr(_common, "container_memory_bytes", lambda: None)
    assert adapter._compute_maxmemory({}) == "1gb"


# ---------------------------------------------------------------------------
# io-threads
# ---------------------------------------------------------------------------
def test_io_threads_override(adapter):
    assert adapter._io_threads({"redis_io_threads": 8}) == 8


def test_io_threads_default_capped(adapter, monkeypatch):
    monkeypatch.setattr(redis_engine.os, "cpu_count", lambda: 64)
    assert adapter._io_threads({}) == 4  # capped


def test_io_threads_default_small_box(adapter, monkeypatch):
    monkeypatch.setattr(redis_engine.os, "cpu_count", lambda: 2)
    assert adapter._io_threads({}) == 2


# ---------------------------------------------------------------------------
# generated redis.conf
# ---------------------------------------------------------------------------
def test_write_conf_contents(adapter, monkeypatch, tmp_path):
    conf = tmp_path / "redis.conf"
    monkeypatch.setattr(redis_engine, "_REDIS_CONF", str(conf))
    monkeypatch.setattr(_common, "container_memory_bytes", lambda: 4 * 1024**3)

    data = tmp_path / "data"
    data.mkdir()
    adapter._write_conf({"password": "sekret-pw"}, 6379, data)

    text = conf.read_text()
    # Durability: database semantics, never silently evict.
    assert "maxmemory-policy noeviction" in text
    assert "allkeys-lru" not in text
    # Big pipes.
    assert "io-threads " in text
    assert "io-threads-do-reads yes" in text
    assert "tcp-backlog 511" in text
    assert "tcp-keepalive 300" in text
    # Persistence still on.
    assert "appendonly yes" in text
    # Auth + lockdown.
    assert "requirepass sekret-pw" in text
    assert "bind 127.0.0.1" in text
    assert "protected-mode yes" in text
    # Sized maxmemory (4 GiB * 0.6 = 2457 mb), not the flat 1gb default.
    assert "maxmemory 2457mb" in text
    # 0600 because the password is in there.
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# _common helpers
# ---------------------------------------------------------------------------
def test_human_bytes():
    assert _common.human_bytes(2 * 1024**3) == "2048mb"
    assert _common.human_bytes(0) == "1mb"  # floor at 1mb, never 0


def test_container_memory_v2(monkeypatch):
    # cgroup v2 memory.max reporting 8 GiB.
    payload = str(8 * 1024**3)

    def fake_read(self, *a, **k):
        if str(self) == "/sys/fs/cgroup/memory.max":
            return payload
        raise OSError("nope")

    monkeypatch.setattr(Path, "read_text", fake_read)
    assert _common.container_memory_bytes() == 8 * 1024**3


def test_container_memory_unbounded_falls_through(monkeypatch):
    # cgroup v2 "max" sentinel → should not be returned as a real limit.
    def fake_read(self, *a, **k):
        if str(self) == "/sys/fs/cgroup/memory.max":
            return "max\n"
        if str(self) == "/proc/meminfo":
            return "MemTotal:       16384 kB\n"
        raise OSError("nope")

    monkeypatch.setattr(Path, "read_text", fake_read)
    # Falls through to MemTotal: 16384 kB = 16 MiB.
    assert _common.container_memory_bytes() == 16384 * 1024
