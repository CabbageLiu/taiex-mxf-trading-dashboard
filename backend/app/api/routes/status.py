from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.config import get_settings
from app.db.engine import session_scope

log = logging.getLogger("taiex.status")

router = APIRouter()


def _now_aware() -> datetime:
    return datetime.now(get_settings().tz)


async def _db_ok() -> bool:
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        log.exception("db health check failed")
        return False


def _ingest_state(ingest) -> tuple[bool, datetime | None, float | None]:
    if ingest is None:
        return False, None, None
    task = getattr(ingest, "_task", None)
    running = task is not None and not task.done()
    last = getattr(ingest, "last_tick", None)
    last_ts = last.ts if last is not None else None
    lag = None
    if last_ts is not None:
        now = _now_aware()
        # Some adapters might emit naive ts — guard.
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)
        lag = (now - last_ts).total_seconds()
    return running, last_ts, lag


def _feed_health(ingest) -> dict[str, Any]:
    """Feed-liveness block. Defaults to healthy when the runner predates the
    watchdog (no `feed_health` method) so older deployments don't read red."""
    if ingest is None or not hasattr(ingest, "feed_health"):
        return {"feed_healthy": True}
    try:
        return ingest.feed_health()
    except Exception:
        log.exception("feed_health() raised in /status")
        return {"feed_healthy": True}


def _strategy_loop_running(strategies) -> bool:
    if strategies is None:
        return False
    tasks = getattr(strategies, "_tasks", None)
    if not tasks:
        return False
    return any(not t.done() for t in tasks)


def _notifier_presence(hub) -> dict[str, bool]:
    out = {"discord": False, "n8n": False, "inapp": False}
    if hub is None:
        return out
    notifiers = getattr(hub, "_notifiers", {})
    settings = get_settings()
    if "discord" in notifiers:
        out["discord"] = bool(settings.discord_webhook_url)
    if "n8n" in notifiers:
        out["n8n"] = bool(settings.n8n_webhook_url)
    if "inapp" in notifiers:
        out["inapp"] = True
    return out


def _per_resolution(ingest) -> dict[str, dict[str, Any]]:
    """Per-resolution dispatch liveness for ops dashboards.

    Surfaces the four signals an operator needs to detect a stalled
    pipeline before signals start getting missed:
    - ``last_bar_close_ts`` — when the runner last finalized a bucket
    - ``queue_depth``       — sum across subscribers (most recent first)
    - ``queue_dropped_total`` — cumulative subscriber-queue overflow drops
    - ``subscribers_count`` — number of consumers attached to the resolution
    """
    if ingest is None:
        return {}
    last_close: dict[str, datetime] = getattr(ingest, "last_close_ts", None) or {}
    dropped: dict[str, int] = getattr(ingest, "dropped_counts", None) or {}
    subscribers: dict = getattr(ingest, "_subscribers", {}) or {}

    keys = set(last_close.keys()) | set(dropped.keys()) | set(subscribers.keys())
    out: dict[str, dict[str, Any]] = {}
    for res in sorted(keys):
        subs = list(subscribers.get(res, ()))
        ts = last_close.get(res)
        out[res] = {
            "last_bar_close_ts": ts.isoformat() if ts is not None else None,
            "queue_depth": sum(getattr(q, "qsize", lambda: 0)() for q in subs),
            "queue_dropped_total": int(dropped.get(res, 0)),
            "subscribers_count": len(subs),
        }
    return out


async def _signals_fired_today() -> int:
    """Count signals persisted today (UTC) — proxy for "is anything firing?"."""
    try:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with session_scope() as s:
            row = (
                await s.execute(
                    text("SELECT COUNT(*) FROM signals WHERE ts >= :start"),
                    {"start": today},
                )
            ).scalar_one()
        return int(row or 0)
    except Exception:
        log.exception("signals_fired_today count failed")
        return 0


def _trend_health(trend_service) -> dict[str, Any]:
    """Surface TrendService presence + latest snapshot for ops dashboards."""
    if trend_service is None:
        return {"configured": False}
    try:
        snap = trend_service.latest()
    except Exception:
        log.exception("trend_service.latest() raised in /status")
        snap = None
    if snap is None:
        return {
            "configured": True,
            "latest_ts": None,
            "last_label": None,
            "last_score": None,
        }
    return {
        "configured": True,
        "latest_ts": snap.ts.isoformat(),
        "last_label": snap.label,
        "last_score": snap.score,
    }


def _detector_state(detector) -> dict[str, Any] | None:
    """Surface the missed-entry detector's last-pass timestamp + counts."""
    if detector is None:
        return None
    return {
        "running": bool(getattr(detector, "running", False)),
        "last_pass_ts": (
            ts.isoformat()
            if (ts := getattr(detector, "last_pass_ts", None)) is not None
            else None
        ),
        "alerts_total": int(getattr(detector, "alerts_total", 0)),
        "autofire_enabled": bool(getattr(detector, "autofire_enabled", False)),
    }


@router.get("/status")
async def status(request: Request) -> dict:
    state = request.app.state
    ingest = getattr(state, "ingest", None)
    hub = getattr(state, "hub", None)
    strategies = getattr(state, "strategies", None)
    tracker = getattr(state, "position_tracker", None)
    detector = getattr(state, "missed_entry_detector", None)
    trend_service = getattr(state, "trend_service", None)

    ingest_running, last_tick_ts, lag = _ingest_state(ingest)
    db_ok = await _db_ok()
    loop_running = _strategy_loop_running(strategies)
    tracker_running = bool(tracker is not None and getattr(tracker, "running", False))
    notifiers = _notifier_presence(hub)
    per_res = _per_resolution(ingest)
    signals_today = await _signals_fired_today() if db_ok else 0
    detector_state = _detector_state(detector)
    trend_state = _trend_health(trend_service)
    feed = _feed_health(ingest)

    # Feed health only fails `ok` when the market is open and ticks have stopped
    # — off-hours silence keeps the dashboard green.
    ok = (
        ingest_running
        and loop_running
        and tracker_running
        and db_ok
        and feed.get("feed_healthy", True)
    )

    payload: dict[str, Any] = {
        "ok": ok,
        "ingest_running": ingest_running,
        "last_tick_ts": last_tick_ts.isoformat() if last_tick_ts else None,
        "ingest_lag_seconds": lag,
        "strategy_loop_running": loop_running,
        "position_tracker_running": tracker_running,
        "db_ok": db_ok,
        "notifiers": notifiers,
        "per_resolution": per_res,
        "signals_fired_today": signals_today,
        "trend_service": trend_state,
        "feed": feed,
    }
    if detector_state is not None:
        payload["missed_entry_detector"] = detector_state
    return payload
