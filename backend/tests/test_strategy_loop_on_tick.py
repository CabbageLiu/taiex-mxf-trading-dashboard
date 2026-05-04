"""StrategyLoop ``bar_update`` → ``Strategy.on_tick`` dispatch.

Verifies that the runtime wiring added alongside the new ``on_tick`` opt-in
hook (``app.strategies.base.TickEvent``) routes ``bar_update`` queue
messages from ``IngestRunner`` to strategies that override the default
no-op ``on_tick``, while strategies that don't override remain isolated.

These tests do **not** require a live database — every async boundary the
loop reaches (``_enabled_configs``, ``_load_bars``, ``_fire``) is mocked
on the loop instance.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import ClassVar
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from pydantic import BaseModel

from app.runner import strategy_loop as loop_mod
from app.runner.strategy_loop import StrategyLoop
from app.strategies.base import BarEvent, Signal, Strategy, TickEvent


class _StubParams(BaseModel):
    pass


class _NoTickStrategy(Strategy):
    """Strategy that does NOT override ``on_tick`` — must be skipped."""

    name: ClassVar[str] = "_test_no_tick_strategy"
    resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams
    indicator_specs: ClassVar[dict[str, dict]] = {}

    bar_calls: ClassVar[int] = 0

    def on_bar(self, ev: BarEvent) -> Signal | None:  # noqa: ARG002
        type(self).bar_calls += 1
        return None


class _TickStrategy(Strategy):
    """Strategy that overrides ``on_tick`` — must receive every dispatch."""

    name: ClassVar[str] = "_test_tick_strategy"
    resolutions: ClassVar[list[str]] = ["1m"]
    tick_resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams
    indicator_specs: ClassVar[dict[str, dict]] = {}

    received: ClassVar[list[TickEvent]] = []
    return_signal: ClassVar[Signal | None] = None

    def on_bar(self, ev: BarEvent) -> Signal | None:  # noqa: ARG002
        return None

    def on_tick(self, ev: TickEvent) -> Signal | None:
        type(self).received.append(ev)
        return type(self).return_signal


class _DummyHub:
    """Minimum surface area for ``StrategyLoop.__init__``; never invoked."""

    async def dispatch(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("hub.dispatch should not be called in these tests")


class _DummyIngest:
    """Minimum surface area for ``StrategyLoop.__init__``; never invoked."""


def _make_loop(monkeypatch: pytest.MonkeyPatch) -> StrategyLoop:
    """Build a ``StrategyLoop`` whose I/O surface is fully mocked.

    - ``_load_bars`` returns a small non-empty DataFrame (`close` column —
      enough for `_compute_indicators`, which iterates `cls.indicator_specs`
      and is empty on our stubs so no real indicator runs).
    - ``_enabled_configs`` returns a config for *both* stub strategies.
    - ``_fire`` is replaced with an ``AsyncMock`` so the test can assert it.
    """
    loop = StrategyLoop(hub=_DummyHub(), ingest=_DummyIngest())  # type: ignore[arg-type]

    sample_bars = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
    )

    async def _fake_load_bars(_resolution: str) -> pd.DataFrame:
        return sample_bars

    async def _fake_enabled_configs() -> dict[str, dict]:
        return {
            _NoTickStrategy.name: {
                "enabled": True,
                "params": {},
                "channels": ["discord"],
            },
            _TickStrategy.name: {
                "enabled": True,
                "params": {},
                "channels": ["discord", "n8n"],
            },
        }

    monkeypatch.setattr(loop, "_load_bars", _fake_load_bars)
    monkeypatch.setattr(loop, "_enabled_configs", _fake_enabled_configs)
    monkeypatch.setattr(loop, "_fire", AsyncMock())

    # Pin the registry to just our two stubs for the duration of the test.
    monkeypatch.setattr(
        loop_mod,
        "all_strategies",
        lambda: {
            _NoTickStrategy.name: _NoTickStrategy,
            _TickStrategy.name: _TickStrategy,
        },
    )
    return loop


@pytest.fixture(autouse=True)
def _reset_class_counters() -> None:
    """Class-level spies are mutable global state — clear between tests."""
    _NoTickStrategy.bar_calls = 0
    _TickStrategy.received = []
    _TickStrategy.return_signal = None
    yield
    _NoTickStrategy.bar_calls = 0
    _TickStrategy.received = []
    _TickStrategy.return_signal = None


@pytest.mark.asyncio
async def test_on_tick_dispatched_only_to_opted_in_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bar_update`` from a queue must route only to strategies that
    actually override ``on_tick``. Non-override strategies see zero call.
    """
    loop = _make_loop(monkeypatch)

    # Drive a real bar_update message through the actual `_loop` body so the
    # branch in `_loop` (msg["type"] == "bar_update") is exercised end-to-end.
    q: asyncio.Queue[dict] = asyncio.Queue()
    ts = datetime(2026, 1, 3, 12, 34, 56)
    await q.put({
        "type": "bar_update",
        "resolution": "1m",
        "bucket": "2026-01-03T12:34:00",
        "ts": ts.isoformat(),
        "price": 102.5,
        "symbol": "MXF",
    })

    # Stop after one message: subscribe returns our queue; once drained, we
    # set `_stop` and put a sentinel so `q.get()` unblocks and the while-loop
    # exits cleanly.
    fake_ingest_subscribe = q

    def _subscribe(_res: str) -> asyncio.Queue:
        return fake_ingest_subscribe

    def _unsubscribe(_res: str, _q: asyncio.Queue) -> None:
        return None

    monkeypatch.setattr(loop._ingest, "subscribe", _subscribe, raising=False)
    monkeypatch.setattr(loop._ingest, "unsubscribe", _unsubscribe, raising=False)

    async def _drive() -> None:
        # Single-shot driver: when the queue empties after our one message,
        # set the stop flag and push a no-op so `q.get()` returns and the
        # while-loop sees `_stop.is_set()` is True.
        while not q.empty():
            await asyncio.sleep(0)
        loop._stop.set()
        await q.put({"type": "noop"})

    driver = asyncio.create_task(_drive())
    await loop._loop("1m")
    await driver

    # Override-strategy got exactly one tick with the right payload.
    assert len(_TickStrategy.received) == 1
    ev = _TickStrategy.received[0]
    assert isinstance(ev, TickEvent)
    assert ev.resolution == "1m"
    assert ev.ts == ts
    assert ev.price == 102.5

    # Non-override strategy must NOT have been called via on_bar in this path,
    # nor (more importantly) via any tick dispatch — `on_bar` is what we'd see
    # if the wrong branch fired.
    assert _NoTickStrategy.bar_calls == 0


@pytest.mark.asyncio
async def test_on_tick_returning_signal_calls_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-None return from ``on_tick`` must reach ``_fire`` with the
    strategy's configured channels."""
    loop = _make_loop(monkeypatch)

    sig = Signal(
        ts=datetime(2026, 1, 3, 12, 34, 56),
        symbol="MXF",
        resolution="1m",
        strategy=_TickStrategy.name,
        side="LONG",
        price=102.5,
        reason="unit-test",
    )
    _TickStrategy.return_signal = sig

    await loop._on_tick("1m", sig.ts, sig.price)

    fire_mock: AsyncMock = loop._fire  # type: ignore[assignment]
    fire_mock.assert_awaited_once_with(sig, ["discord", "n8n"])


@pytest.mark.asyncio
async def test_on_tick_returning_none_does_not_call_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_tick`` returning None must skip ``_fire`` entirely."""
    loop = _make_loop(monkeypatch)
    _TickStrategy.return_signal = None  # explicit; matches default

    await loop._on_tick("1m", datetime(2026, 1, 3, 12, 34, 56), 102.5)

    fire_mock: AsyncMock = loop._fire  # type: ignore[assignment]
    fire_mock.assert_not_awaited()
    # And the override path was indeed entered (so we know the test isn't
    # silently passing because the candidate filter ate the strategy).
    assert len(_TickStrategy.received) == 1


@pytest.mark.asyncio
async def test_on_bar_close_skips_tick_driven_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolutions listed in ``tick_resolutions`` must NOT receive bar_close
    dispatch in live — otherwise the on_bar shim would fire at bucket
    timestamp/close price before the tick path runs (ingest queues
    bar_close ahead of bar_update on the boundary-crossing tick)."""
    loop = _make_loop(monkeypatch)

    await loop._on_bar_close("1m", datetime(2026, 1, 3, 12, 34, 0))

    # Tick-routed resolution: on_bar shim never invoked via bar_close path.
    assert _TickStrategy.received == []
    # Bar-only strategy: still dispatched normally.
    assert _NoTickStrategy.bar_calls == 1


@pytest.mark.asyncio
async def test_on_tick_skipped_when_tick_resolutions_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: a strategy that overrides ``on_tick`` but does not
    declare any ``tick_resolutions`` must not be dispatched on the tick
    path. Opt-in is via the list, not the override alone."""

    class _OrphanTickStrategy(Strategy):
        name: ClassVar[str] = "_test_orphan_tick"
        resolutions: ClassVar[list[str]] = ["1m"]
        # No tick_resolutions declared.
        params_schema: ClassVar[type[BaseModel]] = _StubParams
        indicator_specs: ClassVar[dict[str, dict]] = {}

        received: ClassVar[list] = []

        def on_bar(self, ev: BarEvent) -> Signal | None:  # noqa: ARG002
            return None

        def on_tick(self, ev: TickEvent) -> Signal | None:
            type(self).received.append(ev)
            return None

    loop = StrategyLoop(hub=_DummyHub(), ingest=_DummyIngest())  # type: ignore[arg-type]
    monkeypatch.setattr(
        loop_mod,
        "all_strategies",
        lambda: {_OrphanTickStrategy.name: _OrphanTickStrategy},
    )

    await loop._on_tick("1m", datetime(2026, 1, 3, 12, 34, 56), 102.5)

    assert _OrphanTickStrategy.received == []


@pytest.mark.asyncio
async def test_aux_indicator_specs_populate_ev_indicators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``aux_indicator_specs`` entries must land in ``ev.indicators``
    alongside primary labels on both dispatch paths."""
    primary_bars = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
    )
    aux_bars = pd.DataFrame(
        {"close": [200.0, 201.0]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )

    class _AuxStrategy(Strategy):
        name: ClassVar[str] = "_test_aux"
        resolutions: ClassVar[list[str]] = ["1m"]
        tick_resolutions: ClassVar[list[str]] = ["1m"]
        params_schema: ClassVar[type[BaseModel]] = _StubParams
        indicator_specs: ClassVar[dict[str, dict]] = {}
        aux_indicator_specs: ClassVar[dict[str, dict]] = {
            "macd_5m": {
                "kind": "macd",
                "params": {"fast": 12, "slow": 26, "signal": 9},
                "resolution": "5m",
            },
        }

        received: ClassVar[list[TickEvent]] = []

        def on_bar(self, ev: BarEvent) -> Signal | None:  # noqa: ARG002
            return None

        def on_tick(self, ev: TickEvent) -> Signal | None:
            type(self).received.append(ev)
            return None

    loop = StrategyLoop(hub=_DummyHub(), ingest=_DummyIngest())  # type: ignore[arg-type]

    async def _fake_load_bars(resolution: str) -> pd.DataFrame:
        return aux_bars if resolution == "5m" else primary_bars

    async def _fake_enabled_configs() -> dict[str, dict]:
        return {
            _AuxStrategy.name: {
                "enabled": True,
                "params": {},
                "channels": ["discord"],
            }
        }

    monkeypatch.setattr(loop, "_load_bars", _fake_load_bars)
    monkeypatch.setattr(loop, "_enabled_configs", _fake_enabled_configs)
    monkeypatch.setattr(loop, "_fire", AsyncMock())
    monkeypatch.setattr(
        loop_mod, "all_strategies", lambda: {_AuxStrategy.name: _AuxStrategy}
    )

    await loop._on_tick("1m", datetime(2026, 1, 3, 12, 34, 56), 102.5)

    assert len(_AuxStrategy.received) == 1
    ev = _AuxStrategy.received[0]
    assert "macd_5m" in ev.indicators
    macd_5m = ev.indicators["macd_5m"]
    # Aux indicator was actually computed from aux_bars (not primary_bars).
    assert len(macd_5m) == len(aux_bars)
