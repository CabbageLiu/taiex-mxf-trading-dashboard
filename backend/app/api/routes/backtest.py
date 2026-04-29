from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.backtest.engine import run_backtest

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    strategy: str
    symbol: str | None = None
    start: datetime
    end: datetime
    params: dict[str, Any] | None = None


@router.post("/run")
async def post_backtest(req: BacktestRequest) -> dict:
    try:
        result = await run_backtest(
            strategy_name=req.strategy,
            symbol=req.symbol,
            start=req.start,
            end=req.end,
            params_override=req.params,
        )
    except KeyError:
        raise HTTPException(404, f"unknown strategy: {req.strategy}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return result.model_dump(mode="json")
