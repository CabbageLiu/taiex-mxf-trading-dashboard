"""Slice A — 2m + 10m timeframe support.

Verifies the new resolutions are wired through the in-process resolution
lists and the epoch-aligned ``_bucket_start`` math. No DB required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.api.routes.bars import VALID_RES
from app.backtest.engine import _RES_RANK
from app.ingest.runner import RESOLUTION_DELTAS, RESOLUTIONS, _bucket_start


def test_resolution_deltas_include_2m_10m():
    assert RESOLUTION_DELTAS["2m"] == timedelta(minutes=2)
    assert RESOLUTION_DELTAS["10m"] == timedelta(minutes=10)


def test_valid_res_includes_2m_10m():
    assert "2m" in VALID_RES
    assert "10m" in VALID_RES


def test_bucket_start_2m():
    ts = datetime(2026, 4, 30, 9, 33, 30, tzinfo=UTC)
    assert _bucket_start(ts, "2m") == datetime(2026, 4, 30, 9, 32, tzinfo=UTC)


def test_bucket_start_10m():
    ts = datetime(2026, 4, 30, 9, 37, 15, tzinfo=UTC)
    assert _bucket_start(ts, "10m") == datetime(2026, 4, 30, 9, 30, tzinfo=UTC)


def test_resolution_rank_ordering():
    assert (
        _RES_RANK["1m"]
        < _RES_RANK["2m"]
        < _RES_RANK["5m"]
        < _RES_RANK["10m"]
        < _RES_RANK["15m"]
    )


def test_strategy_loop_subscribes_to_all_resolutions():
    assert isinstance(RESOLUTIONS, list)
    assert len(RESOLUTIONS) == 12
