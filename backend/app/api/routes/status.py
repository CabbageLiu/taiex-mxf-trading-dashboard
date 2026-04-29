from __future__ import annotations

import logging
from datetime import UTC, datetime

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


@router.get("/status")
async def status(request: Request) -> dict:
    state = request.app.state
    ingest = getattr(state, "ingest", None)
    hub = getattr(state, "hub", None)
    strategies = getattr(state, "strategies", None)
    tracker = getattr(state, "position_tracker", None)

    ingest_running, last_tick_ts, lag = _ingest_state(ingest)
    db_ok = await _db_ok()
    loop_running = _strategy_loop_running(strategies)
    tracker_running = bool(tracker is not None and getattr(tracker, "running", False))
    notifiers = _notifier_presence(hub)

    ok = ingest_running and loop_running and tracker_running and db_ok

    return {
        "ok": ok,
        "ingest_running": ingest_running,
        "last_tick_ts": last_tick_ts.isoformat() if last_tick_ts else None,
        "ingest_lag_seconds": lag,
        "strategy_loop_running": loop_running,
        "position_tracker_running": tracker_running,
        "db_ok": db_ok,
        "notifiers": notifiers,
    }
