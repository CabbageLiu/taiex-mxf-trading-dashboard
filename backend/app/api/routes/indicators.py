from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from app.api.routes.bars import load_bars
from app.config import get_settings
from app.indicators.service import available, cache

router = APIRouter(prefix="/indicators", tags=["indicators"])


def _parse_params(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        out = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"params not valid JSON: {e}")
    if not isinstance(out, list):
        raise HTTPException(400, "params must be a JSON array")
    return out


@router.get("/available")
async def list_available() -> dict:
    return {"indicators": available()}


@router.get("")
async def compute_indicators(
    symbol: str | None = Query(default=None),
    res: str = Query(default="1m"),
    kinds: str = Query(default=""),
    params: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10000),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
) -> dict:
    sym = symbol or get_settings().symbol_display
    if end is None:
        end = datetime.now(timezone.utc)
    if start is None:
        start = end - timedelta(days=14)

    requested = [k.strip() for k in kinds.split(",") if k.strip()]
    if not requested:
        raise HTTPException(400, "kinds query param required (e.g. macd,rsi,ma)")

    all_params = _parse_params(params)
    keyed = {p.get("kind"): p.get("params", {}) for p in all_params}

    bars = await load_bars(sym, res, start=start, end=end, limit=limit)
    if bars.empty:
        return {"symbol": sym, "resolution": res, "series": {}}

    series: dict[str, list[dict]] = {}
    for kind in requested:
        try:
            df = cache.get(sym, res, kind, keyed.get(kind, {}), bars)
        except KeyError:
            raise HTTPException(400, f"unknown indicator: {kind}") from None
        rows: list[dict] = []
        for idx, row in df.iterrows():
            rec = {"time": int(idx.timestamp())}
            for col, val in row.items():
                rec[col] = None if val != val else float(val)  # NaN guard
            rows.append(rec)
        series[kind] = rows
    return {"symbol": sym, "resolution": res, "series": series}
