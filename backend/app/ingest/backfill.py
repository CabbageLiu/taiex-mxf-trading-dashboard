"""Historical tick backfill via Shioaji ``api.ticks(contract, date)``.

Live ingest cannot recover ticks missed during outages (laptop closed,
deploy gap). This module fetches Shioaji's historical tick dataset for
the configured rolling contract and idempotently UPSERTs into the
``ticks`` hypertable.

Two entry points:

- `BackfillService.backfill_range(start_date, end_date)` — fetch every
  trading day between (inclusive). Used by `POST /admin/backfill` and
  the startup auto-backfill.
- `BackfillService.backfill_recent(lookback_days)` — scan the last N
  market days for "missing" ones (rows below threshold) and fill those.

Design notes:

* Shioaji historical data is published once the trading day ends. The
  service skips today's date in `_missing_days` since live ingest fills
  the current session.
* The Shioaji SDK returns ``ts: List[int]`` of nanoseconds since epoch
  per the official LLM reference. We convert with
  ``datetime.fromtimestamp(ts_ns / 1e9, tz=Asia/Taipei)``. The first
  trading day's audit (see `scripts/audit_shioaji_ticks.py`) validated
  that the resulting datetimes fall inside known TAIFEX session
  windows; a sanity guard inside ``fetch_day`` re-checks this and
  raises if the unit assumption ever breaks.
* Inserts go through the same `ON CONFLICT DO NOTHING` path as live
  ingest, so re-running over the same window is idempotent.
* `source` is set to ``SHIOAJI_FUTURES_TICK_HISTORICAL`` to distinguish
  these rows from live ticks (``SHIOAJI_FUTURES_TICK``) in
  ``ticks.source``.
* TimescaleDB continuous aggregates have a 30-second refresh policy; no
  explicit refresh is triggered here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters import shioaji_client
from app.adapters.base import Tick
from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Tick as TickRow
from app.ingest.constants import PRICE_FLOOR

log = logging.getLogger("taiex.backfill")

SOURCE = "SHIOAJI_FUTURES_TICK_HISTORICAL"
PRICE_CEILING = 100_000.0
# Serialize Shioaji historical calls; the SDK's thread-safety across the
# live quote thread and synchronous `ticks` calls is undocumented.
_historical_lock = asyncio.Lock()


@dataclass(slots=True)
class DayResult:
    day: date
    fetched: int
    inserted: int  # excludes ON CONFLICT NO-OPs
    error: str | None = None


class ShioajiHistoricalClient:
    """Single-day fetcher for Shioaji `api.ticks`. Independently testable."""

    def __init__(self, *, contract_code: str, tz_name: str = "Asia/Taipei") -> None:
        self._contract_code = contract_code
        self._tz_name = tz_name

    async def fetch_day(self, day: date) -> list[dict[str, Any]]:
        tz = ZoneInfo(self._tz_name)
        api = await shioaji_client.get_api()
        contract = self._resolve_contract(api, self._contract_code)

        async with _historical_lock:
            try:
                ticks = await asyncio.to_thread(
                    api.ticks, contract=contract, date=day.isoformat()
                )
            except Exception:
                shioaji_client.mark_session_broken()
                raise

        ts_list = getattr(ticks, "ts", None) or []
        close_list = getattr(ticks, "close", None) or []
        if not ts_list or not close_list or len(ts_list) != len(close_list):
            log.info(
                "backfill day=%s shioaji returned empty/mismatched payload (ts=%d close=%d)",
                day,
                len(ts_list),
                len(close_list),
            )
            return []

        clean: list[dict[str, Any]] = []
        skipped_floor = 0
        for raw_ts, raw_price in zip(ts_list, close_list, strict=False):
            try:
                ts = datetime.fromtimestamp(int(raw_ts) / 1e9, tz=tz)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if price < PRICE_FLOOR or price > PRICE_CEILING:
                skipped_floor += 1
                continue
            clean.append({"ts": ts, "price": price})

        if clean:
            _assert_within_session(clean[0]["ts"], day)

        log.info(
            "backfill day=%s rows=%d kept=%d skipped_floor=%d",
            day,
            len(ts_list),
            len(clean),
            skipped_floor,
        )
        return clean

    @staticmethod
    def _resolve_contract(api: Any, code: str) -> Any:
        if code.startswith("TXF"):
            try:
                return getattr(api.Contracts.Futures.TXF, code)
            except AttributeError:
                pass
        return api.Contracts.Futures[code]


def _assert_within_session(sample_ts: datetime, day: date) -> None:
    """Crash loudly if the first tick of a day lands outside TAIFEX hours.

    Day session 08:45-13:45 on `day`. Night session opens 15:00 on the
    previous calendar day and runs through 05:00 on `day` (Shioaji
    buckets the entire trading-day session — including the prior
    evening — under the settle-date query).
    """
    local = sample_ts.astimezone(ZoneInfo("Asia/Taipei"))
    t = local.time()
    day_open = dtime(8, 45)
    day_close = dtime(13, 45)
    night_open = dtime(15, 0)
    night_cutoff = dtime(5, 0)

    # Day session: same-day 08:45-13:45.
    if local.date() == day and day_open <= t <= day_close:
        return
    # Prior-evening night session: previous calendar day, time >= 15:00.
    if local.date() == day - timedelta(days=1) and t >= night_open:
        return
    # Overnight wrap of the prior evening's session: `day` (or `day+1`
    # depending on payload framing) at time <= 05:00.
    if t <= night_cutoff and local.date() in (day, day + timedelta(days=1)):
        return
    raise RuntimeError(
        f"shioaji backfill sanity check failed: first tick {local.isoformat()} "
        f"falls outside TAIFEX session for {day.isoformat()}; "
        "ts unit assumption (nanoseconds) may be wrong"
    )


class BackfillService:
    """Coordinates day-by-day backfill, gap detection, and persistence."""

    def __init__(
        self,
        *,
        client: ShioajiHistoricalClient | None = None,
        symbol: str | None = None,
        min_ticks_per_day: int | None = None,
    ) -> None:
        s = get_settings()
        self._symbol = symbol or s.symbol_display
        self._min_ticks_per_day = min_ticks_per_day or s.backfill_min_ticks_per_day
        self._client = client or ShioajiHistoricalClient(
            contract_code=s.shioaji_contract,
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
