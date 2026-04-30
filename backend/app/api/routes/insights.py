"""POST /insights/strategy — AI-generated strategy commentary, server-cached.

Reuses the trade query helpers from :mod:`app.api.routes.trades` and pipes
the result through :func:`app.services.insights.generate_strategy_insight`.

Has a tiny in-memory token bucket (5 requests / minute / (strategy, ip)) to
prevent runaway Anthropic spend if the frontend disable-on-pending logic
fails or the user spams the button. Backed by a dict — no new dependency.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.routes.trades import _parse_dt, _query_trades, _serialize, compute_stats
from app.config import get_settings
from app.services.insights import generate_strategy_insight
from app.services.insights_cache import InsightsCache, make_cache_key

router = APIRouter()


class InsightRequest(BaseModel):
    strategy: str
    # Accept ISO date or datetime strings; parsed via shared `_parse_dt` so the
    # date-only end-bound semantics match `/trades` exactly.
    start: str | None = None
    end: str | None = None
    filter: Literal["all", "win", "loss"] = "all"
    # Optional inline payload — when present, the route skips the `/trades`
    # DB query entirely and uses these directly. Lets the V4 lens model send
    # the same trade slice the UI is rendering, so the AI sees exactly what
    # the user sees. ``trades`` is capped at 200 rows for the prompt budget.
    trades: list[dict] | None = None
    stats: dict | None = None


class InsightResponse(BaseModel):
    cached: bool
    generated_at: datetime
    content: str


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_cache: InsightsCache | None = None


def _get_cache() -> InsightsCache:
    global _cache
    if _cache is None:
        s = get_settings()
        _cache = InsightsCache(
            ttl_seconds=s.insights_cache_ttl_seconds,
            max_entries=s.insights_cache_max_entries,
        )
    return _cache


# ---------------------------------------------------------------------------
# Soft rate limiter — 5 requests / 60s / (strategy, client-ip).
# ---------------------------------------------------------------------------

_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW_SEC = 60.0
# Hard cap on bucket count so an attacker spraying unique IPs cannot grow the
# dict unboundedly. LRU semantics: oldest unused bucket evicted first.
_RATE_LIMIT_MAX_BUCKETS = 1024

# bucket key -> list of monotonic timestamps (ascending). OrderedDict for LRU.
_rate_buckets: OrderedDict[tuple[str, str], list[float]] = OrderedDict()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def _check_rate_limit(strategy: str, ip: str) -> float | None:
    """Returns retry-after seconds if rate-limited, else None."""
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_SEC
    key = (strategy, ip)
    timestamps = _rate_buckets.get(key, [])
    # Drop expired hits.
    timestamps = [ts for ts in timestamps if ts > cutoff]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        oldest = timestamps[0]
        retry_after = max(0.0, _RATE_LIMIT_WINDOW_SEC - (now - oldest))
        _rate_buckets[key] = timestamps
        _rate_buckets.move_to_end(key)
        return retry_after
    timestamps.append(now)
    if not timestamps:
        # Fully expired bucket — drop it to keep memory bounded.
        _rate_buckets.pop(key, None)
        return None
    _rate_buckets[key] = timestamps
    _rate_buckets.move_to_end(key)
    while len(_rate_buckets) > _RATE_LIMIT_MAX_BUCKETS:
        _rate_buckets.popitem(last=False)
    return None


def _coerce_dt(v: Any) -> datetime | None:
    """Best-effort coerce a serialized datetime field to ``datetime``.

    Inline trade rows come in as ISO strings from the frontend; ``compute_stats``
    needs real ``datetime`` objects to compute hold seconds and drawdown
    ordering. Anything unparseable becomes ``None`` (the function tolerates
    that — those rows just don't contribute to ``avg_hold_seconds``).
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/strategy", response_model=InsightResponse)
async def post_strategy_insight(body: InsightRequest, request: Request) -> InsightResponse:
    settings = get_settings()
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not configured on server",
        )

    ip = _client_ip(request)
    retry_after = _check_rate_limit(body.strategy, ip)
    if retry_after is not None:
        retry_after_int = max(1, int(retry_after) + 1)
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded; retry after {retry_after_int}s",
            headers={"Retry-After": str(retry_after_int)},
        )

    # Normalise date inputs through the same helper /trades uses so date-only
    # `end` strings get the inclusive-day-boundary semantic.
    start_dt = _parse_dt(body.start, "start")
    end_dt = _parse_dt(body.end, "end", end_of_day=True)

    # Inline payload path (V4 lens) — skip the DB query and use the rows the
    # caller passed in directly. Cap at 200 to bound the prompt size.
    if body.trades is not None:
        serialized_trades = body.trades[:200]
        if body.stats is not None:
            stats = body.stats
        else:
            # Re-derive stats from the trades via SimpleNamespace adapters so
            # ``compute_stats`` (which reads attribute-style) works without
            # round-tripping through the DB.
            ns_rows: list[Any] = []
            for i, t in enumerate(serialized_trades):
                entry_ts = _coerce_dt(t.get("entry_ts"))
                exit_ts = _coerce_dt(t.get("exit_ts"))
                ns_rows.append(
                    SimpleNamespace(
                        id=t.get("id", i),
                        entry_ts=entry_ts,
                        exit_ts=exit_ts,
                        pnl_points=t.get("pnl_points"),
                    )
                )
            stats = compute_stats(ns_rows)
    else:
        # Pull trades + stats. Cap trade rows at 50 for the prompt budget.
        rows = await _query_trades(
            strategy=body.strategy,
            start=start_dt,
            end=end_dt,
            result=body.filter,
            limit=50,
        )
        serialized_trades = [_serialize(r) for r in rows]
        stats = compute_stats(rows)

    # Cache key: hash a per-trade fingerprint so two distinct distributions
    # with the same trade_count/pnl_total still get distinct cache slots.
    pnl_total = stats.get("pnl_total") or 0.0
    trade_fp = hashlib.sha256(
        "|".join(
            f"{t['id']}:{(t.get('pnl_points') or 0):.4f}"
            for t in sorted(serialized_trades, key=lambda r: r["id"])
        ).encode("utf-8")
    ).hexdigest()
    stats_signature = f"{stats.get('trade_count', 0)}|{float(pnl_total):.4f}|{trade_fp}"
    key = make_cache_key(
        strategy=body.strategy,
        start_iso=start_dt.isoformat() if start_dt else None,
        end_iso=end_dt.isoformat() if end_dt else None,
        filter_=body.filter,
        trade_count=stats.get("trade_count", 0) or 0,
        stats_signature=stats_signature,
    )

    cache = _get_cache()
    hit = cache.get(key)
    if hit is not None:
        content, generated_at = hit
        return InsightResponse(cached=True, generated_at=generated_at, content=content)

    content = await generate_strategy_insight(
        strategy=body.strategy,
        start=start_dt,
        end=end_dt,
        filter=body.filter,
        trade_rows=serialized_trades,
        stats=stats,
    )
    generated_at = cache.put(key, content)
    # `cache.put` returns a UTC-aware datetime; mirror that explicitly here for
    # readers in case the test suite ever shims the cache.
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    return InsightResponse(cached=False, generated_at=generated_at, content=content)
