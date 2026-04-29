"""Position tracker — unit tests.

These do NOT touch the database. We patch the three async DB helpers
(``_open_trade``, ``_close``, ``_side_of``, ``_rehydrate``) on the tracker
instance and assert call ordering / arguments. PnL math is exercised via
``position_tracker._pnl_points``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.notify.hub import NotifierHub
from app.runner.position_tracker import PositionTracker, _pnl_points


def _msg(
    *,
    side: str,
    price: float,
    sid: int,
    ts: str = "2026-04-29T09:00:00+00:00",
    strategy: str = "always_long",
    symbol: str = "MXF",
) -> dict:
    return {
        "type": "signal",
        "id": sid,
        "ts": ts,
        "symbol": symbol,
        "resolution": "1m",
        "strategy": strategy,
        "side": side,
        "price": price,
        "reason": "",
        "payload": {},
    }


@pytest.fixture
def tracker() -> PositionTracker:
    hub = NotifierHub()
    t = PositionTracker(hub=hub)
    # All three db-touching coroutines get mocked per-test below.
    t._open_trade = AsyncMock()
    t._close = AsyncMock()
    t._side_of = AsyncMock()
    return t


def test_pnl_points_long_and_short():
    assert _pnl_points("LONG", 100.0, 110.0, 1.0) == pytest.approx(10.0)
    assert _pnl_points("LONG", 100.0, 90.0, 2.0) == pytest.approx(-20.0)
    assert _pnl_points("SHORT", 100.0, 90.0, 1.0) == pytest.approx(10.0)
    assert _pnl_points("SHORT", 100.0, 110.0, 3.0) == pytest.approx(-30.0)


async def test_long_then_exit_closes_with_positive_pnl(tracker: PositionTracker):
    tracker._open_trade.return_value = 42

    await tracker._handle(_msg(side="LONG", price=100.0, sid=1))
    assert tracker._open[("always_long", "MXF")] == 42
    tracker._open_trade.assert_awaited_once()

    await tracker._handle(_msg(side="EXIT", price=110.0, sid=2))
    tracker._close.assert_awaited_once()
    args, kwargs = tracker._close.call_args
    # Positional: trade_id, ts, price, signal_id
    assert args[0] == 42
    assert args[2] == 110.0
    assert args[3] == 2
    assert ("always_long", "MXF") not in tracker._open


async def test_long_then_short_closes_long_opens_short(tracker: PositionTracker):
    tracker._open_trade.side_effect = [42, 43]
    tracker._side_of.return_value = "LONG"

    await tracker._handle(_msg(side="LONG", price=100.0, sid=1))
    await tracker._handle(_msg(side="SHORT", price=120.0, sid=2))

    # close was called for the existing long
    tracker._close.assert_awaited_once()
    close_args = tracker._close.call_args.args
    assert close_args[0] == 42
    assert close_args[2] == 120.0
    # then a new SHORT trade was opened
    assert tracker._open_trade.await_count == 2
    last_call = tracker._open_trade.call_args_list[-1]
    assert last_call.kwargs["side"] == "SHORT"
    assert last_call.kwargs["price"] == 120.0
    assert tracker._open[("always_long", "MXF")] == 43


async def test_long_then_long_is_noop(tracker: PositionTracker):
    tracker._open_trade.return_value = 42
    tracker._side_of.return_value = "LONG"

    await tracker._handle(_msg(side="LONG", price=100.0, sid=1))
    await tracker._handle(_msg(side="LONG", price=105.0, sid=2))

    # Only one open, no close.
    tracker._open_trade.assert_awaited_once()
    tracker._close.assert_not_awaited()
    assert tracker._open[("always_long", "MXF")] == 42


async def test_duplicate_signal_id_is_idempotent(tracker: PositionTracker):
    tracker._open_trade.return_value = 42

    msg = _msg(side="LONG", price=100.0, sid=1)
    await tracker._handle(msg)
    await tracker._handle(msg)  # exact same id

    tracker._open_trade.assert_awaited_once()
    tracker._close.assert_not_awaited()


async def test_exit_with_no_open_position_is_noop(tracker: PositionTracker):
    await tracker._handle(_msg(side="EXIT", price=100.0, sid=1))
    tracker._close.assert_not_awaited()
    tracker._open_trade.assert_not_awaited()


async def test_short_then_long_closes_short_opens_long(tracker: PositionTracker):
    tracker._open_trade.side_effect = [10, 11]
    tracker._side_of.return_value = "SHORT"

    await tracker._handle(_msg(side="SHORT", price=200.0, sid=1))
    await tracker._handle(_msg(side="LONG", price=180.0, sid=2))

    tracker._close.assert_awaited_once()
    close_args = tracker._close.call_args.args
    assert close_args[0] == 10
    assert close_args[2] == 180.0

    assert tracker._open_trade.await_count == 2
    assert tracker._open_trade.call_args_list[-1].kwargs["side"] == "LONG"
    assert tracker._open[("always_long", "MXF")] == 11


async def test_rehydrates_open_trades_from_db():
    """On start(), tracker should pull rows where exit_ts IS NULL into the
    in-memory position map."""

    hub = NotifierHub()
    t = PositionTracker(hub=hub)

    fake_open_row = type(
        "_R", (), {"id": 99, "strategy": "always_long", "symbol": "MXF"}
    )()

    class _FakeScalars:
        def all(self):
            return [fake_open_row]

    class _FakeResult:
        def scalars(self):
            return _FakeScalars()

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeResult()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_scope():
        yield _FakeSession()

    with patch("app.runner.position_tracker.session_scope", fake_scope):
        await t._rehydrate()

    assert t._open[("always_long", "MXF")] == 99


async def test_handle_skips_unknown_side(tracker: PositionTracker):
    await tracker._handle(_msg(side="WAT", price=100.0, sid=1))
    tracker._open_trade.assert_not_awaited()


async def test_handle_skips_message_missing_strategy(tracker: PositionTracker):
    bad = _msg(side="LONG", price=100.0, sid=1)
    bad["strategy"] = None  # type: ignore[assignment]
    await tracker._handle(bad)
    tracker._open_trade.assert_not_awaited()
