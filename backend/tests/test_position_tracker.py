"""Position tracker — unit tests.

These do NOT touch the database. We patch the three async DB helpers
(``_open_trade``, ``_close``, ``_side_of``, ``_rehydrate``) on the tracker
instance and assert call ordering / arguments. PnL math is exercised via
``position_tracker._pnl_points``.
"""

from __future__ import annotations

from datetime import UTC
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


# ---------- payload threading (V5 Phase A — Slice A5) ----------
#
# Strategies emit Signal.payload = {"entry_ind": {...}} on opens and
# {"exit_ind": {...}} on closes. The tracker copies those snapshots into
# Trade.payload so the /analysis trading log can render KD/MACD/DMI at
# open and close. Same-id replays are filtered upstream (idempotency
# test above), so we only need to verify the payload threading.


_ENTRY_IND = {
    "k": 32.5,
    "d": 28.1,
    "macd": 12.4,
    "signal": 9.8,
    "hist": 2.6,
    "plus_di": 24.0,
    "minus_di": 18.0,
    "adx": 21.0,
}

_EXIT_IND = {
    "k": 78.0,
    "d": 72.0,
    "macd": 5.0,
    "signal": 7.0,
    "hist": -2.0,
    "plus_di": 19.0,
    "minus_di": 24.0,
    "adx": 23.0,
}


async def test_open_trade_threads_entry_ind_into_kwargs(tracker: PositionTracker):
    """LONG signal carrying entry_ind hands it to _open_trade."""
    tracker._open_trade.return_value = 42

    msg = _msg(side="LONG", price=100.0, sid=1)
    msg["payload"] = {"entry_ind": _ENTRY_IND}
    await tracker._handle(msg)

    tracker._open_trade.assert_awaited_once()
    kwargs = tracker._open_trade.call_args.kwargs
    assert kwargs["entry_ind"] == _ENTRY_IND


async def test_close_threads_exit_ind_into_kwargs(tracker: PositionTracker):
    """EXIT signal carrying exit_ind hands it to _close as kwarg."""
    tracker._open_trade.return_value = 42

    open_msg = _msg(side="LONG", price=100.0, sid=1)
    open_msg["payload"] = {"entry_ind": _ENTRY_IND}
    await tracker._handle(open_msg)

    exit_msg = _msg(side="EXIT", price=110.0, sid=2)
    exit_msg["payload"] = {"exit_ind": _EXIT_IND}
    await tracker._handle(exit_msg)

    tracker._close.assert_awaited_once()
    assert tracker._close.call_args.kwargs["exit_ind"] == _EXIT_IND


async def test_open_trade_handles_missing_entry_ind(tracker: PositionTracker):
    """payload={} → _open_trade gets entry_ind=None, no error."""
    tracker._open_trade.return_value = 42

    await tracker._handle(_msg(side="LONG", price=100.0, sid=1))  # payload={}

    tracker._open_trade.assert_awaited_once()
    assert tracker._open_trade.call_args.kwargs["entry_ind"] is None


async def test_close_handles_missing_exit_ind(tracker: PositionTracker):
    """EXIT signal with empty payload → _close gets exit_ind=None."""
    tracker._open_trade.return_value = 42

    await tracker._handle(_msg(side="LONG", price=100.0, sid=1))
    await tracker._handle(_msg(side="EXIT", price=110.0, sid=2))  # payload={}

    tracker._close.assert_awaited_once()
    assert tracker._close.call_args.kwargs["exit_ind"] is None


async def test_flip_threads_exit_ind_to_close_and_entry_ind_to_open(
    tracker: PositionTracker,
):
    """LONG → SHORT flip: SHORT signal with both entry_ind+exit_ind in
    its payload should hand exit_ind to _close (closing the LONG) and
    entry_ind to _open_trade (opening the SHORT)."""
    tracker._open_trade.side_effect = [42, 43]
    tracker._side_of.return_value = "LONG"

    open_msg = _msg(side="LONG", price=100.0, sid=1)
    open_msg["payload"] = {"entry_ind": _ENTRY_IND}
    await tracker._handle(open_msg)

    flip_msg = _msg(side="SHORT", price=120.0, sid=2)
    flip_msg["payload"] = {
        "entry_ind": _EXIT_IND,  # snapshot for the new SHORT entry
        "exit_ind": _EXIT_IND,  # snapshot for closing the existing LONG
    }
    await tracker._handle(flip_msg)

    tracker._close.assert_awaited_once()
    assert tracker._close.call_args.kwargs["exit_ind"] == _EXIT_IND
    # Second open call carries the flip's entry_ind.
    second_open = tracker._open_trade.call_args_list[-1]
    assert second_open.kwargs["side"] == "SHORT"
    assert second_open.kwargs["entry_ind"] == _EXIT_IND


# ---------- DB-side payload persistence (mocked session_scope) ----------


def _patch_session_scope(monkeypatch, fake_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_scope():
        yield fake_session

    monkeypatch.setattr(
        "app.runner.position_tracker.session_scope", fake_scope
    )


class _RecordingSession:
    """Captures Trade objects added and SQL execute() calls."""

    def __init__(self, *, side: str = "LONG", entry_price: float = 100.0,
                 qty: float = 1.0, payload_on_load: dict | None = None,
                 next_id: int = 1) -> None:
        self.added: list = []
        self.executes: list = []
        self.commits: int = 0
        self._next_id = next_id
        # canned scalar_one_or_none for the SELECT-before-update path
        from types import SimpleNamespace
        self._row_for_close = SimpleNamespace(
            side=side, entry_price=entry_price, qty=qty,
            exit_ts=None, payload=dict(payload_on_load or {}),
        )

    def add(self, obj) -> None:
        self.added.append(obj)

    async def execute(self, stmt, params=None):
        self.executes.append((stmt, params))

        class _Result:
            def __init__(self, row):
                self._row = row

            def scalar_one_or_none(self):
                return self._row

        # First execute on _close path is a SELECT — return the canned row
        # for any execute call; the update calls don't read the result.
        return _Result(self._row_for_close)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        # Simulate DB autogen of id.
        obj.id = self._next_id
        self._next_id += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


async def test_open_trade_db_writes_payload_with_entry_ind(monkeypatch):
    hub = NotifierHub()
    t = PositionTracker(hub=hub)
    sess = _RecordingSession(next_id=7)
    _patch_session_scope(monkeypatch, sess)

    from datetime import datetime

    new_id = await t._open_trade(
        strategy="trade_strat_v1",
        symbol="MXF",
        side="LONG",
        ts=datetime(2026, 4, 30, 9, 0, tzinfo=UTC),
        price=100.0,
        signal_id=1,
        entry_ind=_ENTRY_IND,
    )
    assert new_id == 7
    assert len(sess.added) == 1
    row = sess.added[0]
    assert row.payload == {"entry_ind": _ENTRY_IND}


async def test_open_trade_db_writes_empty_payload_when_no_entry_ind(monkeypatch):
    hub = NotifierHub()
    t = PositionTracker(hub=hub)
    sess = _RecordingSession(next_id=8)
    _patch_session_scope(monkeypatch, sess)

    from datetime import datetime

    await t._open_trade(
        strategy="trade_strat_v1",
        symbol="MXF",
        side="LONG",
        ts=datetime(2026, 4, 30, 9, 0, tzinfo=UTC),
        price=100.0,
        signal_id=1,
        entry_ind=None,
    )
    assert sess.added[0].payload == {}


async def test_close_emits_payload_merge_when_exit_ind_present(monkeypatch):
    hub = NotifierHub()
    t = PositionTracker(hub=hub)
    sess = _RecordingSession(side="LONG", entry_price=100.0, qty=1.0,
                             payload_on_load={"entry_ind": _ENTRY_IND})
    _patch_session_scope(monkeypatch, sess)

    from datetime import datetime

    await t._close(
        trade_id=42,
        ts=datetime(2026, 4, 30, 10, 0, tzinfo=UTC),
        price=110.0,
        signal_id=2,
        exit_ind=_EXIT_IND,
    )

    # Three execute calls: SELECT, UPDATE values(...), then the
    # text() merge for exit_ind.
    assert len(sess.executes) == 3
    merge_stmt, merge_params = sess.executes[-1]
    assert "jsonb_build_object" in str(merge_stmt)
    assert merge_params["trade_id"] == 42
    # serialized JSON contains exit_ind keys
    assert "k" in merge_params["exit_ind"]
    assert "78.0" in merge_params["exit_ind"]
    assert sess.commits == 1


async def test_close_skips_payload_merge_when_exit_ind_absent(monkeypatch):
    hub = NotifierHub()
    t = PositionTracker(hub=hub)
    sess = _RecordingSession(side="LONG", entry_price=100.0, qty=1.0)
    _patch_session_scope(monkeypatch, sess)

    from datetime import datetime

    await t._close(
        trade_id=42,
        ts=datetime(2026, 4, 30, 10, 0, tzinfo=UTC),
        price=110.0,
        signal_id=2,
        exit_ind=None,
    )

    # Only the SELECT and the values UPDATE — no merge statement.
    assert len(sess.executes) == 2
    assert sess.commits == 1
