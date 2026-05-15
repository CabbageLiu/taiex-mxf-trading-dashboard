from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel

from app.backtest import engine as engine_mod
from app.backtest.engine import (
    _backtest_cache,
    clear_backtest_cache,
    run_backtest,
)
from app.strategies.base import BarEvent, Strategy
from app.strategies.registry import _registry


class _CacheStubParams(BaseModel):
    a: int = 0


_STATE: dict = {}


class _CacheStubStrat(Strategy):
    name: ClassVar[str] = "_test_bt_cache_stub"
    resolutions: ClassVar[list[str]] = ["30m"]
    params_schema: ClassVar[type[BaseModel]] = _CacheStubParams

    # Class-level counter — every on_bar call increments.
    call_count: ClassVar[int] = 0

    def on_bar(self, ev: BarEvent):
        type(self).call_count += 1
        return None


@pytest.fixture
def stub_strategy(monkeypatch):
    sys.modules[_CacheStubStrat.__module__]._STATE = _STATE
    _registry[_CacheStubStrat.name] = _CacheStubStrat
    _CacheStubStrat.call_count = 0
    clear_backtest_cache()
    yield _CacheStubStrat
    _registry.pop(_CacheStubStrat.name, None)
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
async def test_cache_hit_returns_same_object(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return _fake_bars(3)
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    args = dict(
        strategy_name=_CacheStubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )

    first = await run_backtest(**args)
    calls_after_first = _CacheStubStrat.call_count
    assert calls_after_first > 0

    second = await run_backtest(**args)
    # Second call must hit cache → engine did NOT re-run on_bar.
    assert _CacheStubStrat.call_count == calls_after_first
    # Same BacktestResult instance.
    assert first is second


@pytest.mark.asyncio
async def test_cache_miss_on_different_params(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return _fake_bars(3)
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    base = dict(
        strategy_name=_CacheStubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )

    await run_backtest(**base, params_override={"a": 1})
    after_one = _CacheStubStrat.call_count
    await run_backtest(**base, params_override={"a": 2})
    after_two = _CacheStubStrat.call_count

    # Engine ran a second time because the params hash differs.
    assert after_two > after_one


@pytest.mark.asyncio
async def test_cache_invalidates_on_module_mtime_change(
    stub_strategy, monkeypatch
):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return _fake_bars(3)
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    mtimes = iter([100.0, 200.0])
    monkeypatch.setattr(engine_mod, "_module_mtime", lambda cls: next(mtimes))

    args = dict(
        strategy_name=_CacheStubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )

    await run_backtest(**args)
    after_one = _CacheStubStrat.call_count
    await run_backtest(**args)
    after_two = _CacheStubStrat.call_count

    # Different mtime → cache key differs → engine re-runs.
    assert after_two > after_one


@pytest.mark.asyncio
async def test_cache_lru_eviction(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return _fake_bars(3)
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)
    monkeypatch.setattr(engine_mod, "_BACKTEST_CACHE_MAX", 2)

    base = dict(
        strategy_name=_CacheStubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )

    await run_backtest(**base, params_override={"a": 1})
    await run_backtest(**base, params_override={"a": 2})
    await run_backtest(**base, params_override={"a": 3})

    # Oldest (a=1) should be evicted.
    assert len(_backtest_cache) == 2
    keys = list(_backtest_cache.keys())
    hashes = {k[1] for k in keys}
    assert '{"a": 1}' not in hashes
    assert '{"a": 2}' in hashes
    assert '{"a": 3}' in hashes


@pytest.mark.asyncio
async def test_clear_backtest_cache(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return _fake_bars(3)
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    await run_backtest(
        strategy_name=_CacheStubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )
    assert len(_backtest_cache) > 0

    clear_backtest_cache()
    assert len(_backtest_cache) == 0


@pytest.mark.asyncio
async def test_cache_caches_empty_history(stub_strategy, monkeypatch):
    async def fake_load_bars(symbol, resolution, *, start=None, end=None, limit=None):
        return pd.DataFrame(columns=["open", "high", "low", "close", "tick_count"])
    monkeypatch.setattr(engine_mod, "load_bars", fake_load_bars)

    args = dict(
        strategy_name=_CacheStubStrat.name,
        symbol="MXF",
        start=datetime(2026, 4, 22, tzinfo=UTC),
        end=datetime(2026, 4, 23, tzinfo=UTC),
    )

    first = await run_backtest(**args)
    second = await run_backtest(**args)
    assert first is second
