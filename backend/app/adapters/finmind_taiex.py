from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.adapters.base import Tick
from app.config import get_settings
from app.ingest.constants import PRICE_FLOOR

log = logging.getLogger("taiex.adapter.finmind")

FINMIND_URL = "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"
SOURCE = "FINMIND_FUTURES_SNAPSHOT"


class FinMindTaiexAdapter:
    """Polls FinMind sponsor `taiwan_futures_snapshot` endpoint.

    Snapshot is real-time only: each call returns the latest tick for `data_id`
    (TXF / TMF / CDF). Display symbol is decoupled from source so the rest of
    the stack still labels rows with `symbol_display` (e.g. MXF).
    """

    def __init__(self, display_symbol: str | None = None) -> None:
        s = get_settings()
        self.symbol = display_symbol or s.symbol_display
        self.source = SOURCE
        self._token = s.finmind_token
        self._tz = s.tz
        self._poll = s.poll_interval_sec
        self._open = s.market_open
        self._close = s.market_close
        self._night_open = s.night_session_open
        self._night_close = s.night_session_close
        self._data_id = s.symbol_source
        self._last_seen_ts: datetime | None = None

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        while True:
            now = datetime.now(self._tz)
            if not self._market_open(now):
                until = self._next_open(now)
                wait_s = max((until - now).total_seconds(), 30)
                log.info("market closed; sleeping %.0fs until next open", wait_s)
                await asyncio.sleep(min(wait_s, 60))
                continue

            try:
                rows = await self._fetch()
            except Exception:
                log.exception("FinMind fetch failed; backing off")
                await asyncio.sleep(self._poll * 2)
                continue

            picked = self._pick_front_month(rows)
            new_ticks = self._dedupe([picked] if picked is not None else [])
            for t in new_ticks:
                yield t
            await asyncio.sleep(self._poll)

    async def backfill(self, start: datetime, end: datetime) -> list[Tick]:
        # Snapshot endpoint has no history. Return empty so runner skips backfill.
        log.info("backfill skipped: taiwan_futures_snapshot is real-time only")
        return []

    def _market_open(self, now: datetime) -> bool:
        """True during TAIFEX day session OR after-hours session.

        Day: Mon-Fri 08:45-13:45.
        Night: starts Mon-Fri 15:00 and runs to 05:00 the next morning. Sat
        00:00-05:00 belongs to Friday's night session; Sat after 05:00 and
        all of Sunday are closed.
        """
        naive = now.timetz().replace(tzinfo=None)
        weekday = now.weekday()  # 0=Mon..6=Sun
        if weekday == 6:
            return False
        # Day session
        if weekday < 5 and self._open <= naive <= self._close:
            return True
        # Night session opening half: Mon-Fri at/after 15:00
        if weekday < 5 and naive >= self._night_open:
            return True
        # Night session closing half: Tue-Sat at/before 05:00
        if 1 <= weekday <= 5 and naive <= self._night_close:
            return True
        return False

    def _next_open(self, now: datetime) -> datetime:
        """Walk forward minute-by-minute to the next open moment.

        Bounded loop (max ~3 days) — only invoked when the market is closed,
        so even the worst case (Sat 05:01 → Mon 08:45) stays under 4000 iters.
        """
        candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 * 4):
            if self._market_open(candidate):
                return candidate
            candidate = candidate + timedelta(minutes=1)
        return candidate

    async def _fetch(self) -> list[dict]:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        params: dict[str, str] = {}
        if self._data_id:
            params["data_id"] = self._data_id

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=20) as cli:
                    resp = await cli.get(FINMIND_URL, headers=headers, params=params)
                    resp.raise_for_status()
                    payload = resp.json()
        if "data" not in payload:
            log.warning("unexpected FinMind payload: %s", payload)
            return []
        return payload["data"]

    def _pick_front_month(self, rows: list[dict]) -> dict | None:
        """Pick the front-month outright contract from a snapshot response.

        FinMind `taiwan_futures_snapshot` returns ALL contracts for a product
        (e.g. for `data_id=TXF`: TXFE6 front, TXFR1 rolling-front alias,
        TXFR2 rolling-next, TXFG6 next month, TXFC7 far month, etc.). Their
        prices differ by carry premium — far months trade hundreds of points
        above front. Inserting all rows yields a chart that bounces between
        contracts.

        Picker rules, in priority order:

        1. Prefer the row whose `futures_id` ends with `R1` — TAIFEX's
           continuous rolling-front-month alias (e.g. TXFR1, MTXR1). Most
           semantically correct: it always tracks the front-month outright.
        2. Otherwise the row with the highest `total_volume` (most-liquid =
           front month).
        3. Tie-break by smallest `contract_date` (earliest expiry).

        Returns None for an empty input.
        """
        if not rows:
            return None
        # Rule 1: R1-suffixed rolling alias
        r1 = [r for r in rows if str(r.get("futures_id", "")).endswith("R1")]
        if r1:
            # If multiple R1 rows somehow appear, pick the highest-volume one.
            return max(r1, key=lambda r: r.get("total_volume") or 0)

        # Rule 2 + 3: highest volume, tie-break by smallest numeric contract_date.
        # `contract_date` may be None, NaN, or contain non-digit chars (weekly
        # codes like "202604W5"). Coerce defensively — anything we can't parse
        # as an integer falls back to a sentinel that loses every tie.
        def _cd_key(r: dict) -> int:
            cd = r.get("contract_date")
            if cd is None:
                return 999999
            digits = "".join(ch for ch in str(cd) if ch.isdigit())
            if not digits:
                return 999999
            try:
                return int(digits[:6])  # YYYYMM prefix
            except ValueError:
                return 999999

        return max(
            rows,
            key=lambda r: (r.get("total_volume") or 0, -_cd_key(r)),
        )

    def _rows_to_ticks(self, rows: list[dict]) -> list[Tick]:
        out: list[Tick] = []
        for r in rows:
            raw_ts = r.get("date")
            raw_price = r.get("close")
            if raw_price is None:
                raw_price = r.get("price")
            if raw_ts is None or raw_price is None:
                continue
            try:
                ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=self._tz)
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if price < PRICE_FLOOR:
                log.debug(
                    "skipping sub-floor price tick: ts=%s price=%s (floor=%s)",
                    raw_ts,
                    raw_price,
                    PRICE_FLOOR,
                )
                continue
            out.append(Tick(ts=ts, symbol=self.symbol, price=price, source=self.source))
        return out

    def _dedupe(self, rows: list[dict]) -> list[Tick]:
        ticks = self._rows_to_ticks(rows)
        if not ticks:
            return []
        if self._last_seen_ts is None:
            new = ticks
        else:
            new = [t for t in ticks if t.ts > self._last_seen_ts]
        if new:
            self._last_seen_ts = max(t.ts for t in new)
        return new
