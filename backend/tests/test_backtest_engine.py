from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel

from app.backtest import engine as engine_mod
from app.backtest.engine import (
    BacktestTrade,
    build_equity_curve,
    compute_backtest_stats,
    pair_into_trades,
    run_backtest,
)
from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import _registry


def _sig(ts_off_min: int, side: str, price: float, res: str = "30m") -> Signal:
    return Signal(
        ts=datetime(2026, 4, 22, tzinfo=UTC) + timedelta(minutes=ts_off_min),
        symbol="MXF",
        resolution=res,
        strategy="test",
        side=side,
        price=price,
    )


def _empty_index() -> dict[str, pd.DatetimeIndex]:
    return {"30m": pd.DatetimeIndex([], tz="UTC")}


def test_pair_long_exit_pair():
    trades = pair_into_trades(
        [_sig(0, "LONG", 100.0), _sig(60, "EXIT", 110.0)],
        _empty_index(),
    )
    assert len(trades) == 1
    assert trades[0].side == "LONG"
    assert trades[0].pnl_points == 10.0


def test_pair_long_to_short_reverse():
    trades = pair_into_trades(
        [_sig(0, "LONG", 100.0), _sig(60, "SHORT", 90.0)],
        _empty_index(),
    )
    # First closes LONG @90 (pnl=-10), opens SHORT — second is still open and
    # should not appear in the closed-trades output.
    assert len(trades) == 1
    assert trades[0].side == "LONG"
    assert trades[0].exit_price == 90.0
    assert trades[0].pnl_points == -10.0


def test_pair_same_direction_noop():
    trades = pair_into_trades(
        [_sig(0, "LONG", 100.0), _sig(30, "LONG", 110.0), _sig(60, "EXIT", 105.0)],
        _empty_index(),
    )
    assert len(trades) == 1
    # Entry stays at 100 (second LONG is no-op), exit at 105.
    assert trades[0].entry_price == 100.0
    assert trades[0].exit_price == 105.0


def test_pair_exit_without_position_discarded():
    trades = pair_into_trades([_sig(0, "EXIT", 110.0)], _empty_index())
    assert trades == []


def _bt(idx: int, pnl: float) -> BacktestTrade:
    base = datetime(2026, 4, 22, tzinfo=UTC)
    return BacktestTrade(
        id=idx,
        side="LONG",
        entry_ts=base + timedelta(minutes=idx * 30),
        entry_price=100.0,
        exit_ts=base + timedelta(minutes=idx * 30 + 15),
        exit_price=100.0 + pnl,
        pnl_points=pnl,
        hold_seconds=900.0,
        bars_held=1,
        entry_reason="t",
        exit_reason="t",
    )


def test_compute_stats_basic():
    trades = [_bt(0, 20.0), _bt(1, -10.0), _bt(2, 5.0)]
    stats = compute_backtest_stats(trades)
    assert stats["trade_count"] == 3
    assert stats["win_count"] == 2
    assert stats["loss_count"] == 1
    assert stats["pnl_total"] == 15.0
    assert stats["profit_factor"] == pytest.approx(2.5)  # 25 / 10
    assert stats["largest_win"] == 20.0
    assert stats["largest_loss"] == -10.0


def test_compute_stats_empty():
    stats = compute_backtest_stats([])
    assert stats["trade_count"] == 0
    assert stats["profit_factor"] is None
    assert stats["largest_win"] is None
    assert stats["avg_bars_in_trade"] is None


def test_build_equity_curve_cumulative():
    trades = [_bt(0, 20.0), _bt(1, -10.0), _bt(2, 5.0)]
    curve = build_equity_curve(trades)
    assert [round(p["cumulative_pnl"], 2) for p in curve] == [20.0, 10.0, 15.0]


# ─── End-to-end smoke + state isolation ──────────────────────────────────────

class _StubParams(BaseModel):
    pass


# Module-level state to verify isolation. Backtest engine should snapshot &
# restore this.
_STATE: dict = {}


class _StubStrat(Strategy):
    name: ClassVar[str] = "_test_bt_stub"
    resolutions: ClassVar[list[str]] = ["30m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams

    def on_bar(self, ev: BarEvent):
        _STATE.setdefault((self.name, ev.symbol), []).append(ev.bucket)
        n = len(ev.bars)
        if n == 2:
            return Signal(
                ts=ev.bucket, symbol=ev.symbol, resolution=ev.resolution,
                strategy=self.name, side="LONG", price=float(ev.bars["close"].iloc[-1]),
            )
        if n == 4:
            return Signal(
                ts=ev.bucket, symbol=ev.symbol, resolution=ev.resolution,
                strategy=self.name, side="EXIT", price=float(ev.bars["close"].iloc[-1]),
            )
        return None


@pytest.fixture
def stub_strategy(monkeypatch):
    # Register stub + ensure the engine's _STATE swap finds it on the
    # current test module (which is what `__module__` resolves to).
    from app.backtest.engine import clear_backtest_cache
    sys.modules[_StubStrat.__module__]._STATE = _STATE
    _registry[_StubStrat.name] = _StubStrat
    clear_backtest_cache()
    yield _StubStrat
    _registry.pop(_StubStrat.name, None)
    _STATE.clear()
    clear_backtest_cache()


def _fake_bars(n: int) -> pd.DataFrame:
    idx = pd.date_range("2026-04-22", periods=n, freq="30min", tz="UTC")
    closes = np.arange(100, 100 + n, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes,
         "close": closes, "tick_count": np.full(n, 1)},
        index=idx,
    )


@pytest.mark.asyncio
async def test_run_backtest_end_to_end(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return _fake_bars(5)
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    result = await run_backtest(
        strategy_name=_StubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )
    assert result.strategy == _StubStrat.name
    assert len(result.trades) == 1
    tr = result.trades[0]
    assert tr.side == "LONG"
    assert tr.entry_price == 101.0  # close at index 1 (bars=2)
    assert tr.exit_price == 103.0  # close at index 3 (bars=4)
    assert tr.pnl_points == 2.0
    assert result.stats["trade_count"] == 1
    assert len(result.equity_curve) == 1


@pytest.mark.asyncio
async def test_run_backtest_isolates_module_state(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return _fake_bars(3)
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    # Pre-populate live state for (name, MXF). Engine must NOT leave a polluted
    # state behind after the run.
    _STATE[(_StubStrat.name, "MXF")] = "PRE_EXISTING"

    await run_backtest(
        strategy_name=_StubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )
    # After backtest completes, the original sentinel should be restored.
    assert _STATE.get((_StubStrat.name, "MXF")) == "PRE_EXISTING"


@pytest.mark.asyncio
async def test_run_backtest_404_unknown():
    with pytest.raises(KeyError):
        await run_backtest(
            strategy_name="__no_such__",
            symbol="MXF",
            start=datetime(2026, 4, 22, tzinfo=UTC),
            end=datetime(2026, 4, 23, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_run_backtest_empty_history(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return pd.DataFrame(columns=["open", "high", "low", "close", "tick_count"])
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    result = await run_backtest(
        strategy_name=_StubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )
    assert result.trades == []
    assert result.stats["trade_count"] == 0
