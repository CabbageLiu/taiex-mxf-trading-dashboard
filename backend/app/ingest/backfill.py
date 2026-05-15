"""Historical tick backfill from FinMind `TaiwanFuturesTick`.

The live `taiwan_futures_snapshot` endpoint is real-time only — it has no
history. When the server is down (laptop closed, deploy gap, etc.), ticks
during that window are lost from the live feed. This module fills those
gaps from FinMind's *historical* tick dataset (Backer/Sponsor tier).

Two entry points:

- `BackfillService.backfill_range(start_date, end_date)` — fetch every
  trading day between (inclusive). Used by the manual `POST /admin/backfill`
  endpoint and by the startup auto-backfill.

- `BackfillService.backfill_recent(lookback_days)` — convenience: scan
  the last N market days for "missing" ones (rows below threshold) and
  fill those.

Design notes:

* FinMind `TaiwanFuturesTick` updates **end-of-day**. Today's afternoon
  session does not appear until tonight. The service treats today as
  "incomplete" and only relies on it as a last resort.
* Timestamps in the `date` field are **CST naive** (Asia/Taipei). Verified
  by inspection: rows for `2026-04-29 00:00 to 04:59` correspond to the
  TAIFEX after-hours session that runs from 15:00 CST the previous
  evening to 05:00 CST the next morning.
* Inserts go through the same `ON CONFLICT DO NOTHING` path as the live
  ingest, so re-running over the same window is idempotent.
* `source` is set to `FINMIND_FUTURES_TICK` so historical and live ticks
  are distinguishable in the `ticks.source` column.
* TimescaleDB continuous aggregates have a 30-second refresh policy; we
  do **not** trigger an explicit refresh here — the policy catches up on
  its own, and triggering a manual refresh on a long historical window
  is expensive.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.adapters.base import Tick
from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Tick as TickRow

from app.ingest.constants import PRICE_FLOOR

log = logging.getLogger("taiex.backfill")

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
SOURCE = "FINMIND_FUTURES_TICK"
DATASET = "TaiwanFuturesTick"


@dataclass(slots=True)
class DayResult:
    day: date
    fetched: int
    inserted: int  # excludes ON CONFLICT NO-OPs
    error: str | None = None


class FinmindHistoricalClient:
    """Single-day fetcher for `TaiwanFuturesTick`. Independently testable."""

    def __init__(self, *, token: str, data_id: str, tz_name: str = "Asia/Taipei") -> None:
        self._token = token
        self._data_id = data_id
        self._tz_name = tz_name

    async def fetch_day(self, day: date) -> list[dict[str, Any]]:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(self._tz_name)
        params = {
            "dataset": DATASET,
            "data_id": self._data_id,
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        }
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=60) as cli:
                    resp = await cli.get(FINMIND_DATA_URL, headers=headers, params=params)
                    resp.raise_for_status()
                    payload = resp.json()
        if payload.get("status") == 402:
            raise RuntimeError("FinMind quota exceeded (HTTP 402)")
        rows = payload.get("data") or []

        # Pass 1: filter spreads + floor + parse, keeping contract_date around
        # so pass 2 can pick the dominant outright contract.
        from collections import Counter

        intermediate: list[dict[str, Any]] = []
        skipped_spread = 0
        skipped_floor = 0
        for r in rows:
            if not r.get("date") or r.get("price") is None:
                continue
            cdate = str(r.get("contract_date") or "")
            if "/" in cdate:
                skipped_spread += 1
                continue
            try:
                price = float(r["price"])
            except (TypeError, ValueError):
                continue
            if price < PRICE_FLOOR:
                skipped_floor += 1
                continue
            intermediate.append(
                {
                    "ts": datetime.fromisoformat(str(r["date"])).replace(tzinfo=tz),
                    "price": price,
                    "contract_date": cdate,
                }
            )

        # Pass 2: pick the dominant (most-traded) contract_date — front month.
        # Mixing back-months (`202606`, `202609`, `202612`) injects carry
        # premium and produces price discontinuities. Rows with no
        # contract_date (legacy payload shape) are kept as a free pass so
        # tests + older data continue to work.
        counts = Counter(r["contract_date"] for r in intermediate if r["contract_date"])
        dominant = counts.most_common(1)[0][0] if counts else None
        if dominant is None and len(intermediate) > 1:
            # Degenerate: every row has an empty contract_date. We can't
            # discriminate front vs back-month — flag loudly so we notice
            # if FinMind's payload shape regresses.
            log.warning(
                "backfill day=%s: no contract_date present on any of %d rows; "
                "front-month filter is a no-op for this batch",
                day,
                len(intermediate),
            )
        skipped_off_contract = 0
        clean: list[dict[str, Any]] = []
        for r in intermediate:
            if r["contract_date"] and dominant and r["contract_date"] != dominant:
                skipped_off_contract += 1
                continue
            clean.append({"ts": r["ts"], "price": r["price"]})

        log.info(
            "backfill day=%s rows=%d kept=%d dominant=%s skipped_spread=%d skipped_floor=%d skipped_off_contract=%d",
            day,
            len(rows),
            len(clean),
            dominant,
            skipped_spread,
            skipped_floor,
            skipped_off_contract,
        )
        return clean


class BackfillService:
    """Coordinates day-by-day backfill, gap detection, and persistence."""

    def __init__(
        self,
        *,
        client: FinmindHistoricalClient | None = None,
        symbol: str | None = None,
        min_ticks_per_day: int | None = None,
    ) -> None:
        s = get_settings()
        self._symbol = symbol or s.symbol_display
        self._min_ticks_per_day = min_ticks_per_day or s.backfill_min_ticks_per_day
        self._client = client or FinmindHistoricalClient(
            token=s.finmind_token,
            data_id=s.backfill_data_id,
            tz_name=s.timezone,
        )

    async def backfill_day(self, day: date) -> DayResult:
        try:
            rows = await self._client.fetch_day(day)
        except Exception as exc:
            log.exception("fetch failed for %s", day)
            return DayResult(day=day, fetched=0, inserted=0, error=str(exc))

        if not rows:
            return DayResult(day=day, fetched=0, inserted=0)

        ticks = [
            Tick(ts=r["ts"], symbol=self._symbol, price=r["price"], source=SOURCE)
            for r in rows
        ]
        inserted = await _persist(ticks)
        return DayResult(day=day, fetched=len(ticks), inserted=inserted)

    async def backfill_range(self, start: date, end: date) -> list[DayResult]:
        """Iterate trading days (Mon-Fri) between start..end inclusive."""
        if start > end:
            raise ValueError("start must be <= end")
        results: list[DayResult] = []
        for day in _trading_days(start, end):
            results.append(await self.backfill_day(day))
        return results

    async def backfill_recent(self, lookback_days: int) -> list[DayResult]:
        """Find under-filled days in the last N days and fill them."""
        if lookback_days <= 0:
            return []
        s = get_settings()
        today = datetime.now(s.tz).date()
        start = today - timedelta(days=lookback_days)
        missing = await self._missing_days(start, today)
        if not missing:
            log.info("backfill: no gaps found in last %d days", lookback_days)
            return []
        log.info("backfill: %d missing days found %s", len(missing), missing)
        results: list[DayResult] = []
        for day in missing:
            results.append(await self.backfill_day(day))
            await asyncio.sleep(0.2)  # gentle pacing — quota is generous but be polite
        return results

    async def _missing_days(self, start: date, end: date) -> list[date]:
        """Return market days in [start, end] with fewer than the threshold ticks."""
        s = get_settings()
        # tz-aware bounds covering [start 00:00, end 23:59:59]
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=s.tz)
        end_dt = datetime.combine(end, datetime.max.time(), tzinfo=s.tz)

        async with session_scope() as session:
            stmt = (
                select(
                    func.date(func.timezone(s.timezone, TickRow.ts)).label("d"),
                    func.count().label("n"),
                )
                .where(TickRow.symbol == self._symbol)
                .where(TickRow.ts >= start_dt)
                .where(TickRow.ts <= end_dt)
                .group_by("d")
            )
            rows = (await session.execute(stmt)).all()
        counts: dict[date, int] = {row.d: int(row.n) for row in rows}
        out: list[date] = []
        for day in _trading_days(start, end):
            if day == datetime.now(s.tz).date():
                # Today's session is incomplete in the historical dataset until
                # end-of-day. Skip — live ingest fills the rest.
                continue
            if counts.get(day, 0) < self._min_ticks_per_day:
                out.append(day)
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trading_days(start: date, end: date) -> Iterable[date]:
    """Yield Mon-Fri days in [start, end]. Holiday calendar is V3 work."""
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            yield cur
        cur = cur + timedelta(days=1)


async def _persist(ticks: list[Tick]) -> int:
    """Bulk INSERT ON CONFLICT DO NOTHING. Returns rows actually inserted.

    Postgres caps a single query at 65,535 bind parameters; with 4 columns
    per row that means ~16k rows per INSERT. FinMind historical sessions
    often return 200k+ ticks/day (regular + after-hours), so chunk the
    insert. Each chunk is its own transaction so a partial failure still
    persists the rows that landed before it.
    """
    if not ticks:
        return 0
    CHUNK = 5000
    total = 0
    for i in range(0, len(ticks), CHUNK):
        rows = [
            {"ts": t.ts, "symbol": t.symbol, "price": t.price, "source": t.source}
            for t in ticks[i : i + CHUNK]
        ]
        async with session_scope() as session:
            stmt = pg_insert(TickRow).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["ts", "symbol"])
            result = await session.execute(stmt)
            await session.commit()
        total += int(result.rowcount or 0)
    return total
