"""Position tracker — pairs LONG/SHORT/EXIT/FLAT signals into closed Trade rows.

Subscribes to the in-process InAppNotifier queue (the same fan-out the WS uses)
so there is exactly one source of signal events. State is a dict
``(strategy, symbol) -> open_trade_id``. On startup the tracker rehydrates that
dict from any rows in ``trades`` where ``exit_ts IS NULL``.

PnL formula (points, qty-aware):
- LONG  closed at exit_price : (exit_price - entry_price) * qty
- SHORT closed at exit_price : (entry_price - exit_price) * qty

Idempotency: each incoming message carries the signal row id; we keep a
``last_signal_id`` per (strategy, symbol) pair and silently skip duplicates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.db.engine import session_scope
from app.db.models import Trade
from app.notify.hub import NotifierHub

log = logging.getLogger("taiex.position_tracker")


PositionKey = tuple[str, str]  # (strategy, symbol)


def _parse_ts(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw))


def _pnl_points(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    if side == "LONG":
        return (exit_price - entry_price) * qty
    if side == "SHORT":
        return (entry_price - exit_price) * qty
    raise ValueError(f"unknown side {side!r}")


class PositionTracker:
    def __init__(self, hub: NotifierHub) -> None:
        self._hub = hub
        self._open: dict[PositionKey, int] = {}
        self._last_signal_id: dict[PositionKey, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict] | None = None
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        await self._rehydrate()
        self._queue = self._hub.inapp.subscribe()
        self._task = asyncio.create_task(self._run(), name="position-tracker")

    async def stop(self) -> None:
        self._stop.set()
        if self._queue is not None:
            self._hub.inapp.unsubscribe(self._queue)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._queue = None

    async def _run(self) -> None:
        assert self._queue is not None
        try:
            while not self._stop.is_set():
                msg = await self._queue.get()
                if msg.get("type") != "signal":
                    continue
                try:
                    await self._handle(msg)
                except Exception:
                    log.exception("position tracker failed to handle %s", msg)
        except asyncio.CancelledError:
            raise

    # ---------- Public-ish hook used by tests ----------

    async def _handle(self, signal: dict) -> None:
        """Apply a single inapp signal message to the trade ledger.

        ``signal`` is the dict published by ``InAppNotifier.send`` — it carries
        ``id``, ``strategy``, ``symbol``, ``side``, ``price``, ``ts``, etc.
        """
        strategy = signal.get("strategy")
        symbol = signal.get("symbol")
        side = signal.get("side")
        price = signal.get("price")
        if not strategy or not symbol or side is None:
            return
        key: PositionKey = (strategy, symbol)
        signal_id = signal.get("id")

        # Idempotency — same signal id replayed = no-op.
        if signal_id is not None and self._last_signal_id.get(key) == signal_id:
            return
        if signal_id is not None:
            self._last_signal_id[key] = signal_id

        ts = _parse_ts(signal.get("ts"))
        open_id = self._open.get(key)

        if side in ("EXIT", "FLAT"):
            if open_id is not None and price is not None:
                await self._close(open_id, ts, float(price), signal_id)
                self._open.pop(key, None)
            return

        if side not in ("LONG", "SHORT"):
            return  # unknown side

        if open_id is None:
            if price is None:
                return
            new_id = await self._open_trade(
                strategy=strategy,
                symbol=symbol,
                side=side,
                ts=ts,
                price=float(price),
                signal_id=signal_id,
            )
            self._open[key] = new_id
            return

        # Position already open — check whether we need to flip it.
        existing_side = await self._side_of(open_id)
        if existing_side == side:
            # Same direction — no-op (don't stack).
            return

        # Opposite side: close the existing trade then open a fresh one.
        if price is None:
            return
        await self._close(open_id, ts, float(price), signal_id)
        new_id = await self._open_trade(
            strategy=strategy,
            symbol=symbol,
            side=side,
            ts=ts,
            price=float(price),
            signal_id=signal_id,
        )
        self._open[key] = new_id

    # ---------- DB helpers (one place to patch in tests) ----------

    async def _rehydrate(self) -> None:
        try:
            async with session_scope() as s:
                rows = (
                    await s.execute(
                        select(Trade)
                        .where(Trade.exit_ts.is_(None))
                        .order_by(Trade.id.asc())
                    )
                ).scalars().all()
            duplicates: list[tuple[PositionKey, int, int]] = []
            for r in rows:
                key: PositionKey = (r.strategy, r.symbol)
                existing = self._open.get(key)
                if existing is not None:
                    duplicates.append((key, existing, int(r.id)))
                    # Keep the newest open row id (highest); the older one is
                    # orphaned and must be repaired manually.
                    self._open[key] = max(existing, int(r.id))
                else:
                    self._open[key] = int(r.id)
            if rows:
                log.info("position tracker rehydrated %d open trades", len(rows))
            for key, kept, dup in duplicates:
                log.warning(
                    "position tracker: duplicate open trade rows for %s — "
                    "keeping id=%d, orphaning id=%d. "
                    "DB partial-unique index should prevent this; investigate.",
                    key,
                    kept,
                    dup,
                )
        except Exception:
            log.exception("position tracker rehydrate failed; starting empty")

    async def _open_trade(
        self,
        *,
        strategy: str,
        symbol: str,
        side: str,
        ts: datetime,
        price: float,
        signal_id: int | None,
        qty: float = 1.0,
    ) -> int:
        async with session_scope() as s:
            row = Trade(
                strategy=strategy,
                symbol=symbol,
                side=side,
                entry_ts=ts,
                entry_price=price,
                entry_signal_id=signal_id,
                qty=qty,
                payload={},
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return int(row.id)

    async def _close(
        self,
        trade_id: int,
        ts: datetime,
        price: float,
        signal_id: int | None,
    ) -> None:
        async with session_scope() as s:
            row = (
                await s.execute(select(Trade).where(Trade.id == trade_id))
            ).scalar_one_or_none()
            if row is None or row.exit_ts is not None:
                return
            pnl = _pnl_points(row.side, float(row.entry_price), price, float(row.qty))
            await s.execute(
                update(Trade)
                .where(Trade.id == trade_id)
                .values(
                    exit_ts=ts,
                    exit_price=price,
                    exit_signal_id=signal_id,
                    pnl_points=pnl,
                )
            )
            await s.commit()

    async def _side_of(self, trade_id: int) -> str | None:
        async with session_scope() as s:
            row = (
                await s.execute(select(Trade).where(Trade.id == trade_id))
            ).scalar_one_or_none()
            return row.side if row is not None else None
