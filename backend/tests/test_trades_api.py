"""Trades API stats math — unit tests.

Exercises ``compute_stats`` directly with hand-rolled trade rows so we don't
need a database. The HTTP layer and SQL filter are exercised separately by
the position tracker integration tests in dev.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.api.routes.trades import compute_stats


def _trade(
    *,
    pnl: float | None,
    entry: datetime,
    exit_: datetime | None,
    side: str = "LONG",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=0,
        strategy="always_long",
        symbol="MXF",
        side=side,
        entry_ts=entry,
        entry_price=100.0,
        entry_signal_id=None,
        exit_ts=exit_,
        exit_price=(100.0 + pnl) if pnl is not None else None,
        exit_signal_id=None,
        qty=1.0,
        pnl_points=pnl,
        payload={},
    )


def test_empty_returns_zero_counts_and_null_winrate():
    stats = compute_stats([])
    assert stats["trade_count"] == 0
    assert stats["open_count"] == 0
    assert stats["win_rate"] is None
    assert stats["pnl_total"] == 0.0
    assert stats["pnl_avg_win"] is None
    assert stats["pnl_avg_loss"] is None
    assert stats["max_drawdown"] == 0.0
    assert stats["avg_hold_seconds"] is None


def test_open_trades_counted_separately():
    t0 = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    rows = [
        _trade(pnl=10.0, entry=t0, exit_=t0 + timedelta(minutes=5)),
        _trade(pnl=None, entry=t0 + timedelta(minutes=10), exit_=None),
    ]
    stats = compute_stats(rows)
    assert stats["trade_count"] == 1
    assert stats["open_count"] == 1
    assert stats["win_count"] == 1
    assert stats["loss_count"] == 0


def test_winrate_pnl_and_avg_win_avg_loss():
    t0 = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    rows = [
        _trade(pnl=10.0, entry=t0, exit_=t0 + timedelta(minutes=5)),
        _trade(pnl=20.0, entry=t0, exit_=t0 + timedelta(minutes=10)),
        _trade(pnl=-5.0, entry=t0, exit_=t0 + timedelta(minutes=15)),
        _trade(pnl=-15.0, entry=t0, exit_=t0 + timedelta(minutes=20)),
    ]
    stats = compute_stats(rows)
    assert stats["trade_count"] == 4
    assert stats["win_count"] == 2
    assert stats["loss_count"] == 2
    assert stats["win_rate"] == 0.5
    assert stats["pnl_total"] == 10.0
    assert stats["pnl_avg_win"] == 15.0
    assert stats["pnl_avg_loss"] == -10.0


def test_max_drawdown_is_peak_to_trough():
    """Cumulative pnl curve = [+30, +50, +20, +10, +60].
    Peak after trade 2 = 50; trough after trade 4 = 10; drawdown = 40."""
    base = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    rows = [
        _trade(pnl=30.0, entry=base, exit_=base + timedelta(minutes=1)),
        _trade(pnl=20.0, entry=base, exit_=base + timedelta(minutes=2)),
        _trade(pnl=-30.0, entry=base, exit_=base + timedelta(minutes=3)),
        _trade(pnl=-10.0, entry=base, exit_=base + timedelta(minutes=4)),
        _trade(pnl=50.0, entry=base, exit_=base + timedelta(minutes=5)),
    ]
    stats = compute_stats(rows)
    assert stats["pnl_total"] == 60.0
    assert stats["max_drawdown"] == 40.0


def test_avg_hold_seconds_uses_only_closed_trades():
    base = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    rows = [
        _trade(pnl=10.0, entry=base, exit_=base + timedelta(seconds=60)),
        _trade(pnl=-5.0, entry=base, exit_=base + timedelta(seconds=120)),
        _trade(pnl=None, entry=base, exit_=None),  # open, ignored
    ]
    stats = compute_stats(rows)
    assert stats["avg_hold_seconds"] == 90.0


def test_zero_pnl_counts_as_loss():
    """Even pnl=0 is filed as ``loss`` for filter purposes (we use pnl > 0
    for ``win``). Confirm the stats agree."""
    base = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    rows = [
        _trade(pnl=0.0, entry=base, exit_=base + timedelta(minutes=1)),
        _trade(pnl=10.0, entry=base, exit_=base + timedelta(minutes=2)),
    ]
    stats = compute_stats(rows)
    assert stats["win_count"] == 1
    assert stats["loss_count"] == 1
    assert stats["win_rate"] == 0.5
