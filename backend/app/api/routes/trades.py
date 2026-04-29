from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Trade

router = APIRouter()

ResultFilter = Literal["win", "loss", "all"]
_RESULT_DEFAULT: ResultFilter = "all"


def _parse_dt(s: str | None, name: str, *, end_of_day: bool = False) -> datetime | None:
    """Parse ISO datetime / date string. Naive inputs get the project tz attached.

    For ``end`` bounds, a date-only string (``YYYY-MM-DD``) is interpreted as the
    *start of the next day*, giving an inclusive day boundary when the caller
    queries with ``<`` against the upper bound. This avoids silently dropping
    same-day trades when the frontend sends ``end=today``.
    """
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise HTTPException(400, f"invalid {name}: {e}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_settings().tz)
    if end_of_day and len(s) == 10:
        dt = dt + timedelta(days=1)
    return dt


def _serialize(row: Trade) -> dict:
    hold_seconds = None
    if row.exit_ts is not None and row.entry_ts is not None:
        hold_seconds = (row.exit_ts - row.entry_ts).total_seconds()
    return {
        "id": int(row.id),
        "strategy": row.strategy,
        "symbol": row.symbol,
        "side": row.side,
        "entry_ts": row.entry_ts.isoformat() if row.entry_ts else None,
        "entry_price": float(row.entry_price) if row.entry_price is not None else None,
        "entry_signal_id": int(row.entry_signal_id) if row.entry_signal_id is not None else None,
        "exit_ts": row.exit_ts.isoformat() if row.exit_ts else None,
        "exit_price": float(row.exit_price) if row.exit_price is not None else None,
        "exit_signal_id": int(row.exit_signal_id) if row.exit_signal_id is not None else None,
        "qty": float(row.qty) if row.qty is not None else 1.0,
        "pnl_points": float(row.pnl_points) if row.pnl_points is not None else None,
        "hold_seconds": hold_seconds,
        "payload": row.payload or {},
    }


async def _query_trades(
    strategy: str | None,
    start: datetime | None,
    end: datetime | None,
    result: ResultFilter,
    limit: int,
) -> list[Trade]:
    stmt = select(Trade)
    if strategy:
        stmt = stmt.where(Trade.strategy == strategy)
    if start is not None:
        stmt = stmt.where(Trade.entry_ts >= start)
    if end is not None:
        stmt = stmt.where(Trade.entry_ts < end)
    if result != "all":
        stmt = stmt.where(Trade.exit_ts.is_not(None))
        if result == "win":
            stmt = stmt.where(Trade.pnl_points > 0)
        elif result == "loss":
            stmt = stmt.where(Trade.pnl_points <= 0)
    stmt = stmt.order_by(Trade.entry_ts.desc()).limit(limit)
    async with session_scope() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return list(rows)


def compute_stats(rows: list[Trade]) -> dict:
    """Compute the aggregate payload for /trades/stats from a list of Trade rows.

    Pulled out so unit tests can call it directly with canned rows.
    """
    closed = [r for r in rows if r.exit_ts is not None and r.pnl_points is not None]
    open_count = sum(1 for r in rows if r.exit_ts is None)

    trade_count = len(closed)
    wins = [float(r.pnl_points) for r in closed if float(r.pnl_points) > 0]
    losses = [float(r.pnl_points) for r in closed if float(r.pnl_points) <= 0]
    win_count = len(wins)
    loss_count = len(losses)

    pnl_total = sum(float(r.pnl_points) for r in closed)
    pnl_avg_win = (sum(wins) / win_count) if win_count else None
    pnl_avg_loss = (sum(losses) / loss_count) if loss_count else None
    win_rate = (win_count / trade_count) if trade_count else None

    # Cumulative PnL drawdown — sort by exit_ts so curve is in trade-close order.
    closed_sorted = sorted(closed, key=lambda r: r.exit_ts)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in closed_sorted:
        cum += float(r.pnl_points)
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    avg_hold = None
    if closed:
        holds = [
            (r.exit_ts - r.entry_ts).total_seconds()
            for r in closed
            if r.exit_ts is not None and r.entry_ts is not None
        ]
        if holds:
            avg_hold = sum(holds) / len(holds)

    return {
        "trade_count": trade_count,
        "open_count": open_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "pnl_total": pnl_total,
        "pnl_avg_win": pnl_avg_win,
        "pnl_avg_loss": pnl_avg_loss,
        "max_drawdown": max_dd,
        "avg_hold_seconds": avg_hold,
    }


@router.get("")
async def list_trades(
    strategy: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    result: ResultFilter = _RESULT_DEFAULT,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    start_dt = _parse_dt(start, "start")
    end_dt = _parse_dt(end, "end", end_of_day=True)
    rows = await _query_trades(strategy, start_dt, end_dt, result, limit)
    return [_serialize(r) for r in rows]


@router.get("/stats")
async def trades_stats(
    strategy: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    start_dt = _parse_dt(start, "start")
    end_dt = _parse_dt(end, "end", end_of_day=True)
    # Pull a generous slice — drawdown wants the full curve in the window.
    rows = await _query_trades(strategy, start_dt, end_dt, "all", limit=1000)
    return compute_stats(rows)
