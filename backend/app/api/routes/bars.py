from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from fastapi import APIRouter, Query
from sqlalchemy import text

from app.config import get_settings
from app.db.engine import session_scope
from app.ingest.runner import _bucket_start

router = APIRouter(prefix="/bars", tags=["bars"])

VALID_RES = {"1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "12h", "1d", "1w", "1mo"}


def _view_for(resolution: str) -> str:
    if resolution not in VALID_RES:
        raise ValueError(f"invalid resolution: {resolution}")
    return f"bars_{resolution}"


async def load_bars(
    symbol: str,
    resolution: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    view = _view_for(resolution)
    where = ["symbol = :symbol"]
    params: dict[str, Any] = {"symbol": symbol}
    if start is not None:
        where.append("bucket >= :start")
        params["start"] = start
    if end is not None:
        where.append("bucket <= :end")
        params["end"] = end
    # Exclude the current in-progress bucket — the continuous aggregate
    # refreshes every ~30 s, so the in-progress bucket is up to 30 s stale.
    # The live WebSocket stream is the sole source of the in-progress bar.
    now_utc = datetime.now(UTC)
    cutoff = _bucket_start(now_utc, resolution)
    where.append("bucket < :cutoff")
    params["cutoff"] = cutoff
    where.append("low > 0")
    where.append("high > 0")
    sql = f"SELECT bucket, open, high, low, close, tick_count FROM {view} WHERE " + " AND ".join(
        where
    ) + " ORDER BY bucket"
    if limit is not None:
        sql = (
            f"SELECT * FROM (SELECT bucket, open, high, low, close, tick_count FROM {view} WHERE "
            + " AND ".join(where)
            + " ORDER BY bucket DESC LIMIT :limit) sub ORDER BY bucket"
        )
        params["limit"] = limit
    async with session_scope() as s:
        rows = (await s.execute(text(sql), params)).mappings().all()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "tick_count"])
    df = pd.DataFrame(rows).set_index("bucket")
    return df


@router.get("")
async def get_bars(
    symbol: str = Query(default=None),
    res: str = Query(default="1m"),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int | None = Query(default=500, ge=1, le=10000),
) -> dict:
    sym = symbol or get_settings().symbol_display
    if end is None:
        end = datetime.now(UTC)
    if start is None and limit is None:
        start = end - timedelta(days=2)
    df = await load_bars(sym, res, start=start, end=end, limit=limit)
    bars = [
        {
            "time": int(idx.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "tick_count": int(row["tick_count"]),
        }
        for idx, row in df.iterrows()
    ]
    return {"symbol": sym, "resolution": res, "bars": bars}
