from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.ingest.backfill import (
    BackfillService,
    DayResult,
    ShioajiHistoricalClient,
    _trading_days,
)

TPE = ZoneInfo("Asia/Taipei")


# ---------------------------------------------------------------------------
# _trading_days helper
# ---------------------------------------------------------------------------


def test_trading_days_excludes_weekends():
    # 2026-04-25 = Sat, 2026-04-26 = Sun, 2026-04-27 = Mon
    days = list(_trading_days(date(2026, 4, 24), date(2026, 4, 28)))
    assert days == [
        date(2026, 4, 24),  # Fri
        date(2026, 4, 27),  # Mon
        date(2026, 4, 28),  # Tue
    ]


def test_trading_days_single_weekday():
    days = list(_trading_days(date(2026, 4, 29), date(2026, 4, 29)))
    assert days == [date(2026, 4, 29)]


def test_trading_days_only_weekend_returns_empty():
    days = list(_trading_days(date(2026, 4, 25), date(2026, 4, 26)))
    assert days == []


# ---------------------------------------------------------------------------
# ShioajiHistoricalClient.fetch_day
# ---------------------------------------------------------------------------


def _ns(year: int, month: int, day: int, hh: int, mm: int, ss: int = 0) -> int:
    """Asia/Taipei wall time → nanoseconds since UTC epoch."""
    return int(datetime(year, month, day, hh, mm, ss, tzinfo=TPE).timestamp() * 1e9)


class _FakeShioajiTicks:
    """Mimics the columnar return shape of `api.ticks(...)`."""

    def __init__(self, ts: list[int], close: list[float], volume: list[int] | None = None):
        self.ts = ts
        self.close = close
        self.volume = volume or [1] * len(ts)


class _FakeShioajiApi:
    def __init__(self, payload: _FakeShioajiTicks):
        self._payload = payload
        # `api.Contracts.Futures.TXF.TXFR1` resolves to any sentinel.
        self.Contracts = SimpleNamespace(
            Futures=SimpleNamespace(TXF=SimpleNamespace(TXFR1=object()))
        )

    def ticks(self, contract, date):  # noqa: A002 - matches SDK signature
        return self._payload


def _patch_get_api(payload: _FakeShioajiTicks):
    fake = _FakeShioajiApi(payload)
    return patch(
        "app.ingest.backfill.shioaji_client.get_api",
        new=AsyncMock(return_value=fake),
    )


@pytest.mark.asyncio
async def test_fetch_day_parses_ns_timestamps_and_returns_taipei_aware():
    payload = _FakeShioajiTicks(
        ts=[
            _ns(2026, 4, 29, 9, 0, 0),
            _ns(2026, 4, 29, 9, 0, 1),
        ],
        close=[39200.0, 39201.5],
    )
    client = ShioajiHistoricalClient(contract_code="TXFR1")
    with _patch_get_api(payload):
        rows = await client.fetch_day(date(2026, 4, 29))

    assert len(rows) == 2
    assert rows[0]["ts"] == datetime(2026, 4, 29, 9, 0, 0, tzinfo=TPE)
    assert rows[0]["price"] == 39200.0
    assert rows[1]["ts"] == datetime(2026, 4, 29, 9, 0, 1, tzinfo=TPE)


@pytest.mark.asyncio
async def test_fetch_day_drops_sub_floor_and_above_ceiling():
    payload = _FakeShioajiTicks(
        ts=[
            _ns(2026, 4, 29, 9, 0, 0),
            _ns(2026, 4, 29, 9, 0, 1),
            _ns(2026, 4, 29, 9, 0, 2),
            _ns(2026, 4, 29, 9, 0, 3),
        ],
        close=[39200.0, 500.0, 200_000.0, 39201.5],
    )
    client = ShioajiHistoricalClient(contract_code="TXFR1")
    with _patch_get_api(payload):
        rows = await client.fetch_day(date(2026, 4, 29))

    prices = [r["price"] for r in rows]
    assert prices == [39200.0, 39201.5]


@pytest.mark.asyncio
async def test_fetch_day_returns_empty_for_empty_payload():
    client = ShioajiHistoricalClient(contract_code="TXFR1")
    with _patch_get_api(_FakeShioajiTicks(ts=[], close=[])):
        rows = await client.fetch_day(date(2026, 4, 29))
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_day_returns_empty_when_lengths_mismatch():
    payload = _FakeShioajiTicks(
        ts=[_ns(2026, 4, 29, 9, 0)], close=[39200.0, 39201.0]
    )
    client = ShioajiHistoricalClient(contract_code="TXFR1")
    with _patch_get_api(payload):
        rows = await client.fetch_day(date(2026, 4, 29))
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_day_raises_when_first_tick_outside_session():
    """Sanity check fires if ts unit assumption (nanoseconds) is wrong.

    A `ts` value of 2026-04-29 06:00 Taipei is outside both the day session
    (08:45-13:45) and the previous-night carry window (<=05:00), so the
    sanity guard must raise rather than persist garbage.
    """
    payload = _FakeShioajiTicks(
        ts=[_ns(2026, 4, 29, 6, 0)],
        close=[39200.0],
    )
    client = ShioajiHistoricalClient(contract_code="TXFR1")
    with _patch_get_api(payload):
        with pytest.raises(RuntimeError, match="outside TAIFEX session"):
            await client.fetch_day(date(2026, 4, 29))


@pytest.mark.asyncio
async def test_fetch_day_accepts_night_session_wrap():
    """A tick at 04:00 the morning AFTER the queried date is valid carry."""
    payload = _FakeShioajiTicks(
        ts=[_ns(2026, 4, 30, 4, 0)],
        close=[39200.0],
    )
    client = ShioajiHistoricalClient(contract_code="TXFR1")
    with _patch_get_api(payload):
        rows = await client.fetch_day(date(2026, 4, 29))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_fetch_day_accepts_prior_evening_night_session():
    """Shioaji buckets the prior evening's night session under the next
    settle-date query, so a tick at 23:00 the previous day is valid."""
    payload = _FakeShioajiTicks(
        ts=[_ns(2026, 4, 28, 23, 0)],
        close=[39200.0],
    )
    client = ShioajiHistoricalClient(contract_code="TXFR1")
    with _patch_get_api(payload):
        rows = await client.fetch_day(date(2026, 4, 29))
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# BackfillService.backfill_day — DB persistence is mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_day_inserts_via_persist():
    fake_client = AsyncMock(spec=ShioajiHistoricalClient)
    fake_client.fetch_day.return_value = [
        {"ts": datetime(2026, 4, 29, 9, 0, tzinfo=UTC), "price": 39200.0},
        {"ts": datetime(2026, 4, 29, 9, 0, 1, tzinfo=UTC), "price": 39201.0},
    ]

    svc = BackfillService(client=fake_client, symbol="MXF", min_ticks_per_day=1)

    with patch("app.ingest.backfill._persist", new=AsyncMock(return_value=2)) as mock_persist:
        res = await svc.backfill_day(date(2026, 4, 29))

    assert res == DayResult(day=date(2026, 4, 29), fetched=2, inserted=2, error=None)
    mock_persist.assert_awaited_once()
    ticks_arg = mock_persist.call_args.args[0]
    assert len(ticks_arg) == 2
    assert ticks_arg[0].symbol == "MXF"
    assert ticks_arg[0].source == "SHIOAJI_FUTURES_TICK_HISTORICAL"


@pytest.mark.asyncio
async def test_backfill_day_returns_zero_when_no_rows():
    fake_client = AsyncMock(spec=ShioajiHistoricalClient)
    fake_client.fetch_day.return_value = []

    svc = BackfillService(client=fake_client, symbol="MXF", min_ticks_per_day=1)
    res = await svc.backfill_day(date(2026, 4, 29))

    assert res.fetched == 0
    assert res.inserted == 0
    assert res.error is None


@pytest.mark.asyncio
async def test_backfill_day_swallows_fetch_errors_into_result():
    fake_client = AsyncMock(spec=ShioajiHistoricalClient)
    fake_client.fetch_day.side_effect = RuntimeError("shioaji unauthorized")

    svc = BackfillService(client=fake_client, symbol="MXF", min_ticks_per_day=1)
    res = await svc.backfill_day(date(2026, 4, 29))

    assert res.fetched == 0
    assert res.inserted == 0
    assert res.error is not None and "unauthorized" in res.error


# ---------------------------------------------------------------------------
# backfill_range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_range_iterates_only_weekdays():
    fake_client = AsyncMock(spec=ShioajiHistoricalClient)
    fake_client.fetch_day.return_value = []

    svc = BackfillService(client=fake_client, symbol="MXF", min_ticks_per_day=1)
    # 2026-04-24 = Fri, 25 Sat, 26 Sun, 27 Mon, 28 Tue
    results = await svc.backfill_range(date(2026, 4, 24), date(2026, 4, 28))

    assert [r.day for r in results] == [
        date(2026, 4, 24),
        date(2026, 4, 27),
        date(2026, 4, 28),
    ]
    assert fake_client.fetch_day.await_count == 3


@pytest.mark.asyncio
async def test_backfill_range_rejects_inverted_window():
    svc = BackfillService(client=AsyncMock(spec=ShioajiHistoricalClient), symbol="MXF")
    with pytest.raises(ValueError):
        await svc.backfill_range(date(2026, 4, 29), date(2026, 4, 28))


# ---------------------------------------------------------------------------
# _missing_days — exercises the SQL filter via mocked session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_days_filters_under_threshold_and_skips_today():
    yesterday = date(2026, 4, 28)
    today_overridden = date(2026, 4, 29)

    rows = [
        SimpleNamespace(d=yesterday, n=42),         # under-filled → must backfill
        SimpleNamespace(d=today_overridden, n=10),  # today: must be skipped
    ]

    class FakeResult:
        def all(self):
            return rows

    class FakeSession:
        async def execute(self, _stmt):
            return FakeResult()

    class FakeScope:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_):
            return False

    fake_client = AsyncMock(spec=ShioajiHistoricalClient)
    svc = BackfillService(client=fake_client, symbol="MXF", min_ticks_per_day=1000)

    fixed_now = datetime(2026, 4, 29, 14, 30, tzinfo=TPE)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    with patch("app.ingest.backfill.session_scope", FakeScope), \
         patch("app.ingest.backfill.datetime", FixedDateTime):
        out = await svc._missing_days(date(2026, 4, 27), date(2026, 4, 29))

    assert yesterday in out
    assert today_overridden not in out


# ---------------------------------------------------------------------------
# backfill_recent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_recent_no_op_when_lookback_zero():
    svc = BackfillService(client=AsyncMock(spec=ShioajiHistoricalClient), symbol="MXF")
    out = await svc.backfill_recent(0)
    assert out == []


@pytest.mark.asyncio
async def test_backfill_recent_runs_each_missing_day():
    fake_client = AsyncMock(spec=ShioajiHistoricalClient)
    fake_client.fetch_day.return_value = [
        {"ts": datetime(2026, 4, 28, 9, 0, tzinfo=UTC), "price": 39200.0},
    ]

    svc = BackfillService(client=fake_client, symbol="MXF", min_ticks_per_day=1)
    svc._missing_days = AsyncMock(return_value=[date(2026, 4, 27), date(2026, 4, 28)])  # type: ignore[method-assign]

    with patch("app.ingest.backfill._persist", new=AsyncMock(return_value=1)):
        results = await svc.backfill_recent(7)

    assert [r.day for r in results] == [date(2026, 4, 27), date(2026, 4, 28)]
    assert all(r.inserted == 1 for r in results)
