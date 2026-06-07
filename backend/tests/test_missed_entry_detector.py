"""MissedEntryDetector — replay-based safety net.

Critical correctness checks:
- ``_STATE`` isolation: detector replay must NOT mutate the live module
  state. Verified bit-for-bit before/after a pass.
- Recent-signal short-circuit: when the live loop already fired, detector
  stays silent.
- Auto-fire opt-in: ``MISSED_ENTRY_AUTOFIRE`` flag controls whether the
  signal is dispatched through the hub or only logged.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from pydantic import BaseModel

from app.runner import missed_entry_detector as det_mod
from app.runner.missed_entry_detector import MissedEntryDetector
from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.examples import strat_15k as strat_mod


# ---------------------------------------------------------------------------
# Stub strategy + module-level _STATE (mimicking the production convention).
# ---------------------------------------------------------------------------

_STUB_STATE: dict[tuple[str, str], object] = {}


class _StubParams(BaseModel):
    pass


class _AlignedStrategy(Strategy):
    """Always returns a Signal — simulates "all gates aligned"."""

    name: ClassVar[str] = "_test_aligned_strategy"
    resolutions: ClassVar[list[str]] = ["1m"]
    tick_resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams
    indicator_specs: ClassVar[dict[str, dict]] = {}
    aux_indicator_specs: ClassVar[dict[str, dict]] = {}

    fired_returns: ClassVar[Signal | None] = None
    last_state_seen: ClassVar[object | None] = None

    def on_bar(self, ev: BarEvent) -> Signal | None:
        # Record what _STATE looks like at the moment of replay so the
        # isolation test can assert detector pop'd the live entry first.
        type(self).last_state_seen = _STUB_STATE.get(
            (type(self).name, ev.symbol), "MISSING"
        )
        return type(self).fired_returns


# Wire the stub strategy's "module" to expose `_STATE` for `_swap_state`
# to find (it locates the dict via ``sys.modules[cls.__module__]._STATE``).
# Easiest: register the dict on this module under the canonical name.
_STATE = _STUB_STATE  # noqa: E305 — backtest engine `_swap_state` reads this


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset() -> None:
    _STATE.clear()
    _AlignedStrategy.fired_returns = None
    _AlignedStrategy.last_state_seen = None
    yield
    _STATE.clear()
    _AlignedStrategy.fired_returns = None
    _AlignedStrategy.last_state_seen = None


def _make_detector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bars: pd.DataFrame | None = None,
    autofire: bool = False,
    recent_signal: bool = False,
    enabled: bool = True,
) -> MissedEntryDetector:
    df = bars if bars is not None else pd.DataFrame(
        {"close": [100.0, 101.0, 102.0]},
        index=pd.to_datetime(["2026-05-05T12:00", "2026-05-05T12:15", "2026-05-05T12:30"], utc=True),
    )
    ingest = SimpleNamespace()
    ingest.snapshot_bars = MagicMock(return_value=df)

    async def _fake_ready():
        return None

    ingest.ready = _fake_ready
    hub = SimpleNamespace()
    hub.dispatch = AsyncMock()

    detector = MissedEntryDetector(
        hub=hub,  # type: ignore[arg-type]
        ingest=ingest,  # type: ignore[arg-type]
        bar_window=500,
        interval_seconds=60,
        lookback_minutes=30,
        autofire=autofire,
    )

    async def _enabled():
        if not enabled:
            return {}
        return {
            _AlignedStrategy.name: {
                "enabled": True,
                "params": {},
                "channels": ["discord"],
            }
        }

    monkeypatch.setattr(detector, "_enabled_configs", _enabled)
    monkeypatch.setattr(
        det_mod, "all_strategies", lambda: {_AlignedStrategy.name: _AlignedStrategy}
    )

    async def _recent(_strategy, _resolution, _bucket):
        return recent_signal

    monkeypatch.setattr(detector, "_recent_signal_exists", _recent)

    persist = AsyncMock(return_value=42)
    monkeypatch.setattr(detector, "_persist", persist)
    detector._persist_mock = persist  # type: ignore[attr-defined]

    return detector


def _signal() -> Signal:
    return Signal(
        ts=datetime(2026, 5, 5, 12, 30, 30, tzinfo=UTC),
        symbol="MXF",
        resolution="1m",
        strategy=_AlignedStrategy.name,
        side="LONG",
        price=41266.0,
        reason="entry LONG (test)",
        payload={"fill_hint": "tick"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_records_alert_when_replay_fires_and_no_recent_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _make_detector(monkeypatch)
    _AlignedStrategy.fired_returns = _signal()

    await detector.run_pass()

    assert detector.alerts_total == 1
    # Detect-only mode: no auto-fire dispatch.
    detector._persist_mock.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pass_skips_when_recent_signal_already_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _make_detector(monkeypatch, recent_signal=True)
    _AlignedStrategy.fired_returns = _signal()

    await detector.run_pass()

    # Live loop already fired in lookback window — no alert, no replay.
    assert detector.alerts_total == 0


@pytest.mark.asyncio
async def test_pass_no_signal_when_replay_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _make_detector(monkeypatch)
    _AlignedStrategy.fired_returns = None

    await detector.run_pass()

    assert detector.alerts_total == 0


@pytest.mark.asyncio
async def test_autofire_dispatches_through_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _make_detector(monkeypatch, autofire=True)
    _AlignedStrategy.fired_returns = _signal()

    await detector.run_pass()

    assert detector.alerts_total == 1
    detector._persist_mock.assert_awaited_once()  # type: ignore[attr-defined]
    hub_dispatch: AsyncMock = detector._hub.dispatch  # type: ignore[assignment]
    hub_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_state_isolation_live_state_unchanged_after_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical safety invariant: detector pass must leave live ``_STATE``
    bit-for-bit identical to what it was beforehand. Otherwise the detector
    can clobber an open position or rising-edge latch.
    """
    detector = _make_detector(monkeypatch)
    _AlignedStrategy.fired_returns = _signal()

    # Plant a live position-like sentinel under the strategy's state key.
    sentinel = {"position": "OPEN", "last_long_ready": True, "cooldown_until": None}
    key = (_AlignedStrategy.name, detector._symbol)
    _STATE[key] = sentinel

    await detector.run_pass()

    # Live state restored byte-for-byte.
    assert _STATE[key] is sentinel
    assert _STATE[key] == {"position": "OPEN", "last_long_ready": True, "cooldown_until": None}
    # Detector replay saw the *empty* slot (its own clean state), proving
    # `_swap_state` actually popped the live entry before the strategy ran.
    assert _AlignedStrategy.last_state_seen in (None, "MISSING")


@pytest.mark.asyncio
async def test_pass_skips_when_buffer_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = _make_detector(
        monkeypatch,
        bars=pd.DataFrame(columns=["open", "high", "low", "close", "tick_count"]),
    )
    _AlignedStrategy.fired_returns = _signal()

    await detector.run_pass()

    assert detector.alerts_total == 0


@pytest.mark.asyncio
async def test_recent_signal_lookup_uses_bucket_bounded_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_recent_signal_exists`` must filter by bucket boundary, not by a
    rolling time window. Asserts the SQL is parameterised with
    ``since = bucket - delta`` and ``until = bucket + 2 * delta`` for
    the resolution. A rolling window would let the detector spuriously
    re-alert if ingest stalls and the latest bucket stays pinned past
    the rolling lookback — destructive when autofire is on (would
    persist duplicate signals + create duplicate trades).
    """
    from datetime import timedelta as _td

    detector = MissedEntryDetector(
        hub=SimpleNamespace(),  # type: ignore[arg-type]
        ingest=SimpleNamespace(),  # type: ignore[arg-type]
    )

    captured: dict[str, object] = {}

    class _FakeSession:
        async def execute(self, _stmt, params: dict):
            captured["sql"] = str(_stmt)
            captured["params"] = params

            class _R:
                def first(self_inner) -> None:
                    return None

            return _R()

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *_a):
            return False

    from app.runner import missed_entry_detector as det_local

    monkeypatch.setattr(det_local, "session_scope", lambda: _FakeCtx())

    bucket = datetime(2026, 5, 5, 12, 15, tzinfo=UTC)
    out = await detector._recent_signal_exists("strat_15k", "15m", bucket)

    assert out is False  # _R.first() returned None
    params = captured["params"]
    assert params["strategy"] == "strat_15k"
    assert params["resolution"] == "15m"
    # 15m delta == 15 minutes
    assert params["since"] == bucket - _td(minutes=15)
    assert params["until"] == bucket + _td(minutes=30)
    # SQL is bucket-bounded — no `datetime.now()` placeholder.
    sql = str(captured["sql"]).lower()
    assert "ts >=" in sql.replace(" ", " ").lower() or "ts >= :since" in sql
    assert ":until" in sql or "ts <" in sql.replace(" ", " ").lower()


@pytest.mark.asyncio
async def test_state_isolation_with_real_strat_15k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end isolation against the real strat_15k module's `_STATE`.

    Plants a LIVE _StratState (with position) for strat_15k, drives a
    detector pass that uses strat_15k as its registered strategy, and
    asserts the live position is unchanged after the pass.
    """
    from app.strategies.examples.strat_15k import _PositionState, _StratState

    # Use a small placeholder bars DataFrame the detector will pass to
    # strat_15k. strat_15k.on_bar requires non-NaN `close` and indicator
    # frames; with a 3-row DataFrame and trivially-zero indicators the
    # gates fail and `on_bar` returns None — which is fine for the
    # isolation test (we only care about state untouched).
    df = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0]},
        index=pd.to_datetime(["2026-05-05T12:00", "2026-05-05T12:15", "2026-05-05T12:30"], utc=True),
    )
    ingest = SimpleNamespace()
    ingest.snapshot_bars = MagicMock(return_value=df)

    async def _fake_ready():
        return None

    ingest.ready = _fake_ready
    hub = SimpleNamespace()
    hub.dispatch = AsyncMock()

    detector = MissedEntryDetector(
        hub=hub,  # type: ignore[arg-type]
        ingest=ingest,  # type: ignore[arg-type]
        autofire=False,
    )

    async def _enabled():
        return {
            "strat_15k": {"enabled": True, "params": {}, "channels": []},
        }

    monkeypatch.setattr(detector, "_enabled_configs", _enabled)
    from app.strategies.examples.strat_15k import TradeStrat15K

    monkeypatch.setattr(
        det_mod, "all_strategies", lambda: {"strat_15k": TradeStrat15K}
    )

    async def _recent(*_a, **_k):
        return False

    monkeypatch.setattr(detector, "_recent_signal_exists", _recent)
    monkeypatch.setattr(detector, "_persist", AsyncMock(return_value=99))

    # Plant live state with an open position.
    key = ("strat_15k", detector._symbol)
    pos = _PositionState(
        side="LONG",
        entry_price=41000.0,
        entry_ts=datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC),
        peak_pnl=10.0,
    )
    live = _StratState(position=pos, cooldown_until=None, last_long_ready=True)
    strat_mod._STATE[key] = live

    await detector.run_pass()

    # Position unchanged.
    assert strat_mod._STATE[key] is live
    assert strat_mod._STATE[key].position is pos
    assert strat_mod._STATE[key].position.entry_price == 41000.0
    assert strat_mod._STATE[key].last_long_ready is True
    assert strat_mod._STATE[key].position.peak_pnl == 10.0

    # Cleanup.
    strat_mod._STATE.clear()
