from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

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
