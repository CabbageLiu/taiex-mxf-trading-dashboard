"""IngestRunner — unit tests.

Currently focused on the watchdog that force-closes stale open buckets
when no fresh ticks arrive (quiet trading periods, session boundaries).
The watchdog body is factored as ``_watchdog_tick()`` so tests can drive
a single iteration without spinning the ``_watchdog_loop`` ``asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.ingest.runner import IngestRunner, _bucket_start


class _SilentAdapter:
    """Duck-typed MarketDataAdapter that yields nothing.

    Used for watchdog isolation — we only need the runner instance, not a
    live stream.
    """

    symbol = "MXF"
    source = "TEST"

    async def stream_ticks(self):
        if False:
            yield None  # pragma: no cover

    async def backfill(self, start, end):
        return []


@pytest.mark.asyncio
async def test_watchdog_tick_force_closes_stale_bucket():
    runner = IngestRunner(adapter=_SilentAdapter())
    q = runner.subscribe("1m")

    tz = runner._settings.tz
    # An "open" bucket that is 5 minutes old → past the 3*delta threshold.
    stale_bucket = _bucket_start(datetime.now(tz) - timedelta(minutes=5), "1m")
    runner._open_buckets["1m"] = stale_bucket

    await runner._watchdog_tick()

    # bar_close emitted on the subscriber queue
    msg = q.get_nowait()
    assert msg["type"] == "bar_close"
    assert msg["resolution"] == "1m"
    assert msg["bucket"] == stale_bucket.isoformat()

    # Slot was popped so the next pass doesn't re-emit.
    assert "1m" not in runner._open_buckets


@pytest.mark.asyncio
async def test_watchdog_tick_keeps_fresh_bucket():
    """A bucket within the 3*delta grace window must NOT be force-closed."""
    runner = IngestRunner(adapter=_SilentAdapter())
    q = runner.subscribe("1m")

    tz = runner._settings.tz
    # Bucket that started <3 minutes ago — still inside the grace window.
    fresh_bucket = _bucket_start(datetime.now(tz), "1m")
    runner._open_buckets["1m"] = fresh_bucket

    await runner._watchdog_tick()

    assert q.empty()
    assert runner._open_buckets["1m"] == fresh_bucket


@pytest.mark.asyncio
async def test_watchdog_tick_keeps_bucket_inside_3_delta_grace():
    """A bucket inside the 3*delta grace window must NOT be force-closed.

    Place the bucket directly so we don't fight `_bucket_start`'s floor
    behaviour: pin it to ``now - 90s`` (well inside the 180s grace) and
    confirm the watchdog leaves it open.
    """
    runner = IngestRunner(adapter=_SilentAdapter())
    q = runner.subscribe("1m")

    tz = runner._settings.tz
    bucket = _bucket_start(datetime.now(tz), "1m") - timedelta(seconds=90)
    runner._open_buckets["1m"] = bucket

    await runner._watchdog_tick()

    assert q.empty()
    assert runner._open_buckets["1m"] == bucket


@pytest.mark.asyncio
async def test_watchdog_loop_recovers_from_emit_failure():
    """A subscriber whose ``put_nowait`` raises a non-QueueFull error must not
    kill ``_watchdog_loop``; the next iteration must still process.

    The inner ``_watchdog_tick`` doesn't try to swallow generic exceptions —
    that's the loop's job (the broad ``except`` in ``_watchdog_loop``). This
    test verifies the failure path works end-to-end: explode on tick #1,
    recover and force-close cleanly on tick #2.
    """
    runner = IngestRunner(adapter=_SilentAdapter())

    tz = runner._settings.tz
    runner._open_buckets["1m"] = _bucket_start(
        datetime.now(tz) - timedelta(minutes=10), "1m"
    )

    class _ExplodingQueue:
        calls = 0

        def put_nowait(self, _msg):
            type(self).calls += 1
            raise RuntimeError("boom")

    bad = _ExplodingQueue()
    runner._subscribers["1m"].add(bad)  # type: ignore[arg-type]

    # First _watchdog_tick must propagate; _watchdog_loop's except clause is
    # what swallows it in the production code path.
    with pytest.raises(RuntimeError):
        await runner._watchdog_tick()
    assert _ExplodingQueue.calls == 1

    # Simulate _watchdog_loop's recovery: remove the bad subscriber, add a
    # working one, re-run — the runner must still be operable.
    runner._subscribers["1m"].discard(bad)  # type: ignore[arg-type]
    runner._open_buckets["5m"] = _bucket_start(
        datetime.now(tz) - timedelta(minutes=30), "5m"
    )
    q5 = runner.subscribe("5m")
    await runner._watchdog_tick()
    msg = q5.get_nowait()
    assert msg["type"] == "bar_close"
    assert msg["resolution"] == "5m"
    assert "5m" not in runner._open_buckets


@pytest.mark.asyncio
async def test_handle_tick_ignores_already_closed_bucket():
    """Once the watchdog retires a bucket, a delayed tick must not re-seed it.

    Otherwise `_handle_tick` would set `_open_buckets[res] = bucket` for the
    closed bucket, and the *next* tick crossing into a fresh bucket would
    trigger a second `bar_close` for the already-retired one.
    """
    from app.adapters.base import Tick

    runner = IngestRunner(adapter=_SilentAdapter())
    runner._persist = AsyncMock()  # type: ignore[method-assign]
    q = runner.subscribe("1m")

    tz = runner._settings.tz
    # Stale 1m bucket → watchdog closes it.
    stale = _bucket_start(datetime.now(tz) - timedelta(minutes=10), "1m")
    runner._open_buckets["1m"] = stale
    await runner._watchdog_tick()

    # Drain the watchdog's bar_close so the next assertion is clean.
    while not q.empty():
        q.get_nowait()

    # A delayed tick arrives for the SAME (already-closed) 1m bucket.
    delayed = Tick(ts=stale + timedelta(seconds=1), symbol="MXF", price=18000.0, source="TEST")
    await runner._handle_tick(delayed)

    # The closed bucket must NOT be re-seeded for any resolution that already
    # tombstoned it. For 1m specifically: no new bar_close, no _open_buckets
    # entry pointing at the retired bucket.
    assert "1m" not in runner._open_buckets or runner._open_buckets["1m"] != stale
    # No bar_close for the retired bucket should appear.
    while not q.empty():
        msg = q.get_nowait()
        assert not (msg["type"] == "bar_close" and msg["bucket"] == stale.isoformat()), (
            "duplicate bar_close emitted for an already-retired bucket"
        )


@pytest.mark.asyncio
async def test_mark_closed_bounds_tombstones_to_4():
    """Tombstone history must be bounded so it cannot grow unboundedly."""
    runner = IngestRunner(adapter=_SilentAdapter())
    base = datetime(2025, 1, 1)
    for i in range(10):
        runner._mark_closed("1m", base + timedelta(minutes=i))
    assert len(runner._closed_buckets["1m"]) == 4
    # The oldest entries are evicted; the most recent 4 remain.
    assert runner._closed_buckets["1m"][0] == base + timedelta(minutes=6)
    assert runner._closed_buckets["1m"][-1] == base + timedelta(minutes=9)


@pytest.mark.asyncio
async def test_start_creates_watchdog_task_and_stop_cancels_it():
    runner = IngestRunner(adapter=_SilentAdapter())
    # Avoid running the heavy _backfill_recent path inside _run().
    runner._backfill_recent = AsyncMock()  # type: ignore[method-assign]

    await runner.start()
    try:
        assert runner._watchdog_task is not None
        assert not runner._watchdog_task.done()
    finally:
        await runner.stop()

    assert runner._watchdog_task is None


@pytest.mark.asyncio
async def test_watchdog_loop_cancels_cleanly():
    runner = IngestRunner(adapter=_SilentAdapter())
    runner._stop.clear()

    task = asyncio.create_task(runner._watchdog_loop())
    # Let the loop reach its first sleep.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
