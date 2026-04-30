from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.db.engine import session_scope
from app.db.models import Alert

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def recent_alerts(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    async with session_scope() as s:
        rows = (
            await s.execute(select(Alert).order_by(Alert.ts.desc()).limit(limit))
        ).scalars().all()
    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat(),
            "signal_id": r.signal_id,
            "channel": r.channel,
            "status": r.status,
            "http_code": r.http_code,
            "error": r.error,
        }
        for r in rows
    ]


@router.get("/stats")
async def alerts_stats() -> dict[str, dict]:
    """Per-channel delivery health for the 通知遞送 UI panel.

    Aggregates ``alerts`` by ``(channel, status)`` and returns a flat map
    keyed by channel: ``{channel: {sent, failed, last_ts}}``. ``last_ts``
    is the most recent attempt across both statuses for the channel.
    """
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(
                    Alert.channel,
                    Alert.status,
                    func.count(Alert.id).label("count"),
                    func.max(Alert.ts).label("last_ts"),
                ).group_by(Alert.channel, Alert.status)
            )
        ).all()
    out: dict[str, dict] = {}
    for r in rows:
        bucket = out.setdefault(r.channel, {"sent": 0, "failed": 0, "last_ts": None})
        if r.status == "ok":
            bucket["sent"] += int(r.count)
        else:
            bucket["failed"] += int(r.count)
        last = r.last_ts.isoformat() if r.last_ts else None
        if last and (bucket["last_ts"] is None or last > bucket["last_ts"]):
            bucket["last_ts"] = last
    return out
