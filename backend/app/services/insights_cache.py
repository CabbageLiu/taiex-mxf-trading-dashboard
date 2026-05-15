"""Bounded TTL LRU cache for AI insight responses.

Pure-Python in-process cache. Restart drops the cache — that's intentional
(the V2 plan documents that no Redis dependency is desired). Keyed on a
SHA-256 of (strategy, start, end, filter, trade_count, stats_signature) so
new trades or filter changes invalidate automatically.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from datetime import UTC, datetime


def make_cache_key(
    *,
    strategy: str,
    start_iso: str | None,
    end_iso: str | None,
    filter_: str,
    trade_count: int,
    stats_signature: str,
) -> str:
    """Stable cache key. SHA-256 hex digest.

    `stats_signature` should be a small deterministic string capturing whatever
    aggregate signal we want to invalidate on (e.g. f"{trade_count}|{pnl_total:.4f}").
    """
    raw = "|".join(
        [
            strategy,
            start_iso or "",
            end_iso or "",
            filter_,
            str(trade_count),
            stats_signature,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class InsightsCache:
    """Bounded TTL LRU.

    - `get(key)` returns `(content, generated_at)` if present and not expired,
      else None. Touching an entry promotes it to most-recently-used.
    - `put(key, content)` stores the entry with `time.monotonic()` as insertion
      time. Evicts the oldest entry if size exceeds `max_entries`.
    """

    def __init__(self, *, ttl_seconds: int, max_entries: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl = ttl_seconds
        self._max = max_entries
        # value = (content_str, mono_inserted_at, generated_at_dt)
        self._store: OrderedDict[str, tuple[str, float, datetime]] = OrderedDict()

    def get(self, key: str) -> tuple[str, datetime] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        content, mono_at, generated_at = entry
        now = time.monotonic()
        if now - mono_at > self._ttl:
            # Expired — drop it.
            self._store.pop(key, None)
            return None
        # Touch (LRU promotion).
        self._store.move_to_end(key)
        return content, generated_at

    def put(self, key: str, content: str) -> datetime:
        now_mono = time.monotonic()
        generated_at = datetime.now(UTC)
        # Replace any prior entry, then push to most-recent end.
        self._store[key] = (content, now_mono, generated_at)
        self._store.move_to_end(key)
        # Evict oldest until within bounds.
        while len(self._store) > self._max:
            self._store.popitem(last=False)
        return generated_at

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()
