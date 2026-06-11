"""Trend service REST surface.

Read-only endpoints that expose ``TrendService``'s latest in-memory snapshot
(``/trend``) and a point-in-time historical lookup against the ``trends``
hypertable (``/trend/at?ts=...``). ``/trend/history`` is intentionally out of
scope for the initial rollout and tracked under deferred backlog.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/trend", tags=["trend"])


def _serialize(snap) -> dict:
    return {
        "ts": snap.ts.isoformat(),
        "symbol": snap.symbol,
        "resolution": snap.resolution,
        "ema20": snap.ema20,
        "ema50": snap.ema50,
        "plus_di": snap.plus_di,
        "minus_di": snap.minus_di,
        "adx": snap.adx,
        "direction": snap.direction,
        "score": snap.score,
        "label": snap.label,
    }


@router.get("")
async def get_latest(request: Request) -> dict:
    svc = getattr(request.app.state, "trend_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="trend_service not configured")
    snap = svc.latest()
    if snap is None:
        raise HTTPException(status_code=404, detail="no trend snapshot yet")
    return _serialize(snap)


@router.get("/at")
async def get_at(request: Request, ts: datetime = Query(...)) -> dict:
    svc = getattr(request.app.state, "trend_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="trend_service not configured")
    snap = await svc.get_at(ts)
    if snap is None:
        raise HTTPException(status_code=404, detail="no trend snapshot at or before ts")
    return _serialize(snap)
