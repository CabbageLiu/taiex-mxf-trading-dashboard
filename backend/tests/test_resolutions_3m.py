"""Slice A1 — 3m timeframe support.

Verifies the new resolution is wired through the in-process resolution
lists and the epoch-aligned ``_bucket_start`` math. No DB required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.api.routes.bars import VALID_RES
from app.backtest.engine import _RES_RANK
from app.ingest.runner import RESOLUTION_DELTAS, _bucket_start


def test_resolution_delta_3m():
    assert RESOLUTION_DELTAS["3m"] == timedelta(minutes=3)


def test_valid_res_includes_3m():
    assert "3m" in VALID_RES


def test_bucket_start_3m():
    ts = datetime(2026, 4, 30, 9, 31, 30, tzinfo=UTC)
    assert _bucket_start(ts, "3m") == datetime(2026, 4, 30, 9, 30, tzinfo=UTC)


def test_resolution_rank_includes_3m():
    assert _RES_RANK["2m"] < _RES_RANK["3m"] < _RES_RANK["5m"]
