"""Unit tests for app.services.insights_cache.

Covers:
  - put then get returns the stored content + timestamp
  - TTL expiry: monkeypatch time.monotonic to advance past TTL → get returns None
  - Max-entries eviction: insert N+1 entries, oldest evicted
  - Cache key changes when trade_count changes
  - Cache key changes when filter changes
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import insights_cache as ic_mod
from app.services.insights_cache import InsightsCache, make_cache_key


def test_put_then_get_returns_content_and_timestamp() -> None:
    cache = InsightsCache(ttl_seconds=60, max_entries=10)
    generated_at = cache.put("k1", "hello world")
    assert isinstance(generated_at, datetime)

    hit = cache.get("k1")
    assert hit is not None
    content, ts = hit
    assert content == "hello world"
    assert ts == generated_at


def test_get_miss_returns_none() -> None:
    cache = InsightsCache(ttl_seconds=60, max_entries=10)
    assert cache.get("missing") is None


def test_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clock = {"now": 1_000.0}

    def fake_monotonic() -> float:
        return fake_clock["now"]

    monkeypatch.setattr(ic_mod.time, "monotonic", fake_monotonic)

    cache = InsightsCache(ttl_seconds=30, max_entries=10)
    cache.put("k1", "payload")

    # Within TTL.
    fake_clock["now"] = 1_010.0
    assert cache.get("k1") is not None

    # Just past TTL.
    fake_clock["now"] = 1_031.0
    assert cache.get("k1") is None

    # And the entry is purged from the underlying store.
    assert len(cache) == 0


def test_max_entries_eviction_drops_oldest() -> None:
    cache = InsightsCache(ttl_seconds=600, max_entries=3)
    cache.put("a", "A")
    cache.put("b", "B")
    cache.put("c", "C")
    # Oldest is 'a'. Adding 'd' should evict it.
    cache.put("d", "D")

    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None
    assert cache.get("d") is not None
    assert len(cache) == 3


def test_lru_get_promotes_recently_used() -> None:
    cache = InsightsCache(ttl_seconds=600, max_entries=3)
    cache.put("a", "A")
    cache.put("b", "B")
    cache.put("c", "C")
    # Touch 'a' so it's most-recent. Now insertion order LRU is b → c → a.
    assert cache.get("a") is not None
    cache.put("d", "D")
    # 'b' should have been evicted, not 'a'.
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert cache.get("d") is not None


def test_cache_key_changes_when_trade_count_changes() -> None:
    base = dict(
        strategy="always_long",
        start_iso="2026-04-01T00:00:00",
        end_iso="2026-04-29T00:00:00",
        filter_="all",
        stats_signature="ignored",
    )
    k1 = make_cache_key(**base, trade_count=10)
    k2 = make_cache_key(**base, trade_count=11)
    assert k1 != k2


def test_cache_key_changes_when_filter_changes() -> None:
    base = dict(
        strategy="always_long",
        start_iso=None,
        end_iso=None,
        trade_count=5,
        stats_signature="sig",
    )
    k_all = make_cache_key(**base, filter_="all")
    k_win = make_cache_key(**base, filter_="win")
    k_loss = make_cache_key(**base, filter_="loss")
    assert k_all != k_win
    assert k_win != k_loss
    assert k_all != k_loss


def test_cache_key_stable_for_same_inputs() -> None:
    args = dict(
        strategy="ema_cross",
        start_iso="2026-01-01T00:00:00",
        end_iso="2026-04-29T00:00:00",
        filter_="win",
        trade_count=42,
        stats_signature="42|123.4500",
    )
    assert make_cache_key(**args) == make_cache_key(**args)


def test_invalid_constructor_args() -> None:
    with pytest.raises(ValueError):
        InsightsCache(ttl_seconds=0, max_entries=10)
    with pytest.raises(ValueError):
        InsightsCache(ttl_seconds=60, max_entries=0)
