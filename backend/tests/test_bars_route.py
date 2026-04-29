"""`/bars` route — unit tests.

Verifies that ``load_bars`` excludes the in-progress bucket from the SQL
result. The continuous aggregate's view of the in-progress bucket lags up
to ~30 s behind reality (the `add_continuous_aggregate_policy` runs every
30 s) so we filter it out at the query layer; the live WebSocket stream is
the sole source of the in-progress bar on the frontend.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.api.routes.bars import load_bars
from app.ingest.runner import _bucket_start


class _FakeResult:
    def mappings(self):
        return self

    def all(self):
        return []


class _CapturingSession:
    """Fake AsyncSession that records every (sql, params) execute()."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        # ``stmt`` is a sqlalchemy TextClause; pull the raw SQL out.
        sql = str(getattr(stmt, "text", stmt))
        self.calls.append((sql, dict(params or {})))
        return _FakeResult()


def _scope_factory(session: _CapturingSession):
    @asynccontextmanager
    async def fake_scope():
        yield session

    return fake_scope


@pytest.mark.asyncio
async def test_load_bars_excludes_in_progress_bucket_no_limit():
    sess = _CapturingSession()
    with patch("app.api.routes.bars.session_scope", _scope_factory(sess)):
        await load_bars("MXF", "1m", limit=None)

    assert len(sess.calls) == 1
    sql, params = sess.calls[0]
    assert "bucket < :cutoff" in sql
    assert "cutoff" in params
    cutoff = params["cutoff"]
    assert isinstance(cutoff, datetime)
    # The cutoff must be the start of the *current* 1m bucket in UTC, so
    # everything strictly before it (i.e. closed buckets) is returned.
    expected = _bucket_start(datetime.now(UTC), "1m")
    # Allow ±1 minute jitter — the call sites compute now_utc independently.
    assert abs((cutoff - expected).total_seconds()) <= 60


@pytest.mark.asyncio
async def test_load_bars_excludes_in_progress_bucket_with_limit():
    sess = _CapturingSession()
    with patch("app.api.routes.bars.session_scope", _scope_factory(sess)):
        await load_bars("MXF", "5m", limit=100)

    assert len(sess.calls) == 1
    sql, params = sess.calls[0]
    assert "bucket < :cutoff" in sql
    assert params["limit"] == 100
    cutoff = params["cutoff"]
    expected = _bucket_start(datetime.now(UTC), "5m")
    assert abs((cutoff - expected).total_seconds()) <= 5 * 60


@pytest.mark.asyncio
async def test_load_bars_cutoff_aligns_to_resolution():
    """The cutoff must be a bucket boundary, not just `now`."""
    sess = _CapturingSession()
    with patch("app.api.routes.bars.session_scope", _scope_factory(sess)):
        await load_bars("MXF", "15m", limit=None)

    _sql, params = sess.calls[0]
    cutoff: datetime = params["cutoff"]
    # 15m buckets start at minute % 15 == 0, with seconds/micros zeroed.
    assert cutoff.minute % 15 == 0
    assert cutoff.second == 0
    assert cutoff.microsecond == 0
