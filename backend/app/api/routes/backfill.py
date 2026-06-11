"""POST /admin/backfill — manual historical-tick backfill trigger.

Usage example:

    curl -X POST 'http://127.0.0.1:8000/admin/backfill?start=2026-04-20&end=2026-04-29'

Returns one row per market day attempted, with `fetched` (rows from
Shioaji `api.ticks`) and `inserted` (rows actually persisted; ON CONFLICT
NO-OPs are excluded). Useful for backtesting setup — pull a wide
historical window once, then run strategies against the resulting `ticks`
/ continuous aggregates.

Auth: none in V2 (documented gap; see `V3_plan.md`). Tailscale-only deploys
are fine; do not expose this surface publicly without first wiring the
`alert_secret` header check.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.ingest.backfill import BackfillService, DayResult

router = APIRouter()


def _serialize(r: DayResult) -> dict[str, Any]:
    return {
        "day": r.day.isoformat(),
        "fetched": r.fetched,
        "inserted": r.inserted,
        "error": r.error,
    }


@router.post("/backfill")
async def post_backfill(
    start: str = Query(..., description="Inclusive start date YYYY-MM-DD"),
    end: str | None = Query(
        default=None,
        description="Inclusive end date YYYY-MM-DD; defaults to today",
    ),
) -> dict[str, Any]:
    settings = get_settings()
    if settings.shioaji_api_key is None or settings.shioaji_secret_key is None:
        raise HTTPException(
            503, "SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY not configured on server"
        )
    try:
        start_d = date.fromisoformat(start)
    except ValueError as e:
        raise HTTPException(400, f"invalid start: {e}") from None
    if end is None:
        end_d = datetime.now(settings.tz).date()
    else:
        try:
            end_d = date.fromisoformat(end)
        except ValueError as e:
            raise HTTPException(400, f"invalid end: {e}") from None
    if start_d > end_d:
        raise HTTPException(400, "start must be <= end")

    service = BackfillService()
    results = await service.backfill_range(start_d, end_d)
    return {
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "days": [_serialize(r) for r in results],
        "total_inserted": sum(r.inserted for r in results),
        "total_fetched": sum(r.fetched for r in results),
    }
