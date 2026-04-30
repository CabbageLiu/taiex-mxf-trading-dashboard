"""GET /signals — recent signal rows for the 即時訊號 panel mount-seed.

Lightweight read of the ``signals`` table, sorted by ts desc, optionally
filtered by ``strategy`` and ``since=``. The live stream still flows over
the WebSocket; this endpoint only seeds the initial view so the panel is
not empty after a refresh.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.db.engine import session_scope
from app.db.models import Signal as SignalRow

router = APIRouter(prefix="/signals", tags=["signals"])


def _serialize(r: SignalRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "ts": r.ts.isoformat(),
        "symbol": r.symbol,
        "resolution": r.resolution,
        "strategy": r.strategy,
        "side": r.side,
        "price": float(r.price) if r.price is not None else None,
        "payload": r.payload or {},
    }


@router.get("")
async def list_signals(
    strategy: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO datetime; signals strictly >= this"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(400, f"invalid since: {e}") from None

    stmt = select(SignalRow).order_by(SignalRow.ts.desc()).limit(limit)
    if strategy:
        stmt = stmt.where(SignalRow.strategy == strategy)
    if since_dt is not None:
        stmt = stmt.where(SignalRow.ts >= since_dt)
    async with session_scope() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [_serialize(r) for r in rows]
