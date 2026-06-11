"""Shioaji (SinoPac) live tick adapter.

Bridges Shioaji's push-based callback API into the ``AsyncIterator[Tick]``
contract that ``IngestRunner`` consumes.

Threading model
---------------
``api.quote.subscribe(...)`` registers callbacks that fire on the SDK's
internal quote thread, NOT the asyncio loop. We forward each tick onto the
loop with ``loop.call_soon_threadsafe(_enqueue, tick_dict)``; ``_enqueue``
runs on the loop, so it can manipulate the ``asyncio.Queue`` safely.

Backpressure
------------
The bridge queue has a hard cap (``settings.shioaji_queue_maxsize``). On
overflow we drop the *oldest* tick and log a warning. This trades a small
amount of OHLC fidelity (a missed high or low for the in-progress bar)
against unbounded memory growth if downstream persistence stalls. Open +
close prices survive under steady-state load since the first and last
ticks in a bar are the ones that move slowest relative to the bar
boundary. Sustained ``shioaji queue full`` warnings = escalate; the fix is
to widen the queue or speed up ``_persist``, NOT to remove the cap.

Reconnect idempotency
---------------------
Callbacks are registered ONCE at adapter init (not per-reconnect) so the
SDK's internal callback registry stays single-entry. The ``on_event``
handler only re-subscribes the *contract* on event 4 (Reconnected); it
does not re-register the callback closure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters import shioaji_client
from app.adapters.base import Tick
from app.config import get_settings
from app.ingest.constants import PRICE_FLOOR

log = logging.getLogger("taiex.adapter.shioaji")

SOURCE = "SHIOAJI_FUTURES_TICK"


class ShioajiFuturesAdapter:
    """Push-based MXF/TXF futures tick stream via Shioaji."""

    def __init__(self, display_symbol: str | None = None) -> None:
        settings = get_settings()
        self.symbol = display_symbol or settings.symbol_display
        self.source = SOURCE
        self._tz = ZoneInfo(settings.timezone)
        self._contract_code = settings.shioaji_contract
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=settings.shioaji_queue_maxsize
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._api: Any = None
        self._contract: Any = None
        self._callbacks_registered = False
        self._subscribe_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        # Monotonic session generation. Bumped on every (re)establish so that
        # callbacks closing over a retired SDK instance can detect they are
        # stale and no-op — see `_register_callbacks`.
        self._session_gen = 0

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        await self._ensure_session()
        while True:
            tick_dict = await self._queue.get()
            tick = self._build_tick(tick_dict)
            if tick is None:
                continue
            yield tick

    async def backfill(self, start: datetime, end: datetime) -> list[Tick]:
        # Historical backfill is handled by `ShioajiHistoricalClient` via the
        # `BackfillService` path; the live adapter does not own that channel.
        log.info("backfill skipped: handled by ShioajiHistoricalClient")
        return []

    # ------------------------------------------------------------------
    # Session setup
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> None:
        if self._api is not None and self._contract is not None:
            return
        async with self._session_lock:
            if self._api is not None and self._contract is not None:
                return
            await self._establish_locked()

    async def _establish_locked(self) -> None:
        """Bring up api + contract + callbacks + subscription.

        Caller MUST already hold ``_session_lock`` — this never re-acquires it
        (so ``reconnect`` can drive a full teardown + bring-up under a single
        lock acquisition without deadlocking against ``_ensure_session``).
        """
        self._loop = asyncio.get_running_loop()
        self._session_gen += 1
        self._api = await shioaji_client.get_api()
        self._contract = self._resolve_contract(self._api, self._contract_code)
        if not self._callbacks_registered:
            self._register_callbacks(self._api, self._session_gen)
            self._callbacks_registered = True
        await self._subscribe()

    async def reconnect(self) -> None:
        """Full teardown + fresh session.

        Used by the feed-health watchdog when ticks stop flowing during market
        hours. The SDK's own auto-reconnect operates on the *same* instance and
        can get stuck "down" permanently (observed 2026-06-01); recovery
        therefore requires a brand-new ``sj.Shioaji()`` instance. We
        ``logout()`` first to release the SinoPac connection slot (5/personId
        cap) before ``get_api()`` builds + logs in a fresh one.
        """
        log.warning("shioaji adapter forced reconnect: tearing down session")
        async with self._session_lock:
            # Retire the current generation *before* teardown so any callback
            # from the dying SDK instance that fires during logout() no-ops
            # immediately (closes the TOCTOU window where it could still match
            # the live generation and enqueue a stale tick).
            self._session_gen += 1
            try:
                await shioaji_client.logout()
            except Exception:
                log.exception("reconnect: logout failed; continuing")
            self._api = None
            self._contract = None
            self._callbacks_registered = False  # new instance → re-register
            self._drain_queue()  # drop ticks buffered from the dead session
            await self._establish_locked()
        log.info("shioaji adapter reconnect complete (gen=%d)", self._session_gen)

    def _drain_queue(self) -> None:
        """Discard any ticks buffered from a now-retired session so post-
        reconnect silence math isn't skewed by stale (older-ts) ticks."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    @staticmethod
    def _resolve_contract(api: Any, code: str) -> Any:
        # SinoPac exposes rolling aliases as attributes (e.g.
        # `api.Contracts.Futures.TXF.TXFR1`). Fall back to dict-style lookup
        # for explicit contract codes.
        if code.startswith("TXF"):
            try:
                return getattr(api.Contracts.Futures.TXF, code)
            except AttributeError:
                pass
        return api.Contracts.Futures[code]

    async def _subscribe(self) -> None:
        import shioaji as sj  # type: ignore[import-not-found]

        async with self._subscribe_lock:
            try:
                await asyncio.to_thread(
                    self._api.quote.subscribe,
                    self._contract,
                    quote_type=sj.constant.QuoteType.Tick,
                )
            except Exception:
                shioaji_client.mark_session_broken()
                raise
        log.info("shioaji subscribed: contract=%s", self._contract_code)

    def _register_callbacks(self, api: Any, gen: int) -> None:
        from shioaji import (  # type: ignore[import-not-found]
            BidAskFOPv1,  # noqa: F401  (re-export sanity)
            Exchange,  # noqa: F401
            TickFOPv1,
        )

        @api.on_tick_fop_v1()
        def _on_tick(_exchange: Any, tick: TickFOPv1) -> None:  # pragma: no cover - SDK thread
            # Ignore callbacks fired by a retired SDK instance after a forced
            # reconnect bumped the generation.
            if gen != self._session_gen:
                return
            payload = self._extract_tick_payload(tick)
            if payload is None:
                return
            self._dispatch(payload)

        @api.quote.on_event
        def _on_event(  # pragma: no cover - SDK thread
            resp_code: int, event_code: int, info: str, event: str
        ) -> None:
            log.info(
                "shioaji event resp=%s code=%s info=%s event=%s gen=%s",
                resp_code,
                event_code,
                info,
                event,
                gen,
            )
            if gen != self._session_gen:
                return  # stale instance — a fresh session has superseded this one
            if event_code == 4 and self._loop is not None:
                # Reconnected: re-subscribe the contract from the loop.
                asyncio.run_coroutine_threadsafe(self._subscribe(), self._loop)

    # ------------------------------------------------------------------
    # Tick plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tick_payload(tick: Any) -> dict[str, Any] | None:
        try:
            return {"datetime": tick.datetime, "close": tick.close}
        except AttributeError:
            log.warning("shioaji tick missing expected attrs: %r", tick)
            return None

    def _dispatch(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue, payload)

    def _enqueue(self, payload: dict[str, Any]) -> None:
        # Runs on the asyncio loop, safe to manipulate the queue.
        try:
            self._queue.put_nowait(payload)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:  # pragma: no cover - shouldn't happen
            return
        log.warning("shioaji queue full; dropped oldest tick")

    def _build_tick(self, payload: dict[str, Any]) -> Tick | None:
        raw_ts = payload.get("datetime")
        raw_price = payload.get("close")
        if raw_ts is None or raw_price is None:
            return None
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            log.warning("shioaji tick has non-numeric price: %r", raw_price)
            return None
        if price < PRICE_FLOOR:
            return None
        ts = self._normalize_ts(raw_ts)
        if ts is None:
            return None
        return Tick(ts=ts, symbol=self.symbol, price=price, source=self.source)

    def _normalize_ts(self, raw: Any) -> datetime | None:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else raw.replace(tzinfo=self._tz)
        # Some shioaji builds expose `ts` as an int (ns since epoch); be
        # defensive in case the callback payload shape drifts.
        if isinstance(raw, int):
            try:
                return datetime.fromtimestamp(raw / 1e9, tz=self._tz)
            except (OverflowError, OSError, ValueError):
                return None
        log.warning("shioaji tick has unrecognized ts type: %r", raw)
        return None


__all__ = ["ShioajiFuturesAdapter", "SOURCE"]
