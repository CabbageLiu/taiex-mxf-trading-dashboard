"""Status route observability fields (Phase 3.1 + 3.2).

Verifies the response contains the per-resolution liveness map, signals
counter, and missed-entry detector summary that ops dashboards rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.api.routes.status as status_mod


class _Q:
    def __init__(self, n: int) -> None:
        self._n = n

    def qsize(self) -> int:
        return self._n


@pytest.mark.asyncio
async def test_per_resolution_surfaces_buffer_and_queue_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest = SimpleNamespace()
    ts_15m = datetime(2026, 5, 6, 12, 30, tzinfo=UTC)
    ingest.last_close_ts = {"15m": ts_15m}
    ingest.dropped_counts = {"15m": 3, "1m": 0}
    ingest._subscribers = {"15m": {_Q(2)}, "1m": {_Q(0), _Q(0)}}

    out = status_mod._per_resolution(ingest)

    assert "15m" in out and "1m" in out
    assert out["15m"]["last_bar_close_ts"] == ts_15m.isoformat()
    assert out["15m"]["queue_depth"] == 2
    assert out["15m"]["queue_dropped_total"] == 3
    assert out["15m"]["subscribers_count"] == 1
    assert out["1m"]["queue_dropped_total"] == 0
    assert out["1m"]["subscribers_count"] == 2


def test_per_resolution_handles_missing_ingest() -> None:
    assert status_mod._per_resolution(None) == {}


def test_detector_state_running_with_fields() -> None:
    detector = SimpleNamespace(
        running=True,
        last_pass_ts=datetime(2026, 5, 6, 12, 31, tzinfo=UTC),
        alerts_total=4,
        autofire_enabled=False,
    )
    out = status_mod._detector_state(detector)
    assert out is not None
    assert out["running"] is True
    assert out["last_pass_ts"].endswith("12:31:00+00:00")
    assert out["alerts_total"] == 4
    assert out["autofire_enabled"] is False


def test_detector_state_none_when_unset() -> None:
    assert status_mod._detector_state(None) is None


@pytest.mark.asyncio
async def test_status_endpoint_includes_new_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the route handler returns the new top-level fields."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(status_mod.router)

    # Stub out DB + signals counter — no live database needed.
    async def _ok() -> bool:
        return True

    async def _count() -> int:
        return 7

    monkeypatch.setattr(status_mod, "_db_ok", _ok)
    monkeypatch.setattr(status_mod, "_signals_fired_today", _count)

    ingest = SimpleNamespace()
    ingest._task = SimpleNamespace(done=lambda: False)
    ingest.last_tick = SimpleNamespace(ts=datetime(2026, 5, 6, 12, 0, tzinfo=UTC))
    ingest.last_close_ts = {"15m": datetime(2026, 5, 6, 12, 0, tzinfo=UTC)}
    ingest.dropped_counts = {"15m": 0}
    ingest._subscribers = {"15m": {_Q(0)}}

    strategies = SimpleNamespace(_tasks=[SimpleNamespace(done=lambda: False)])
    tracker = SimpleNamespace(running=True)
    hub = SimpleNamespace(_notifiers={"inapp": object()})
    detector = SimpleNamespace(
        running=True,
        last_pass_ts=datetime(2026, 5, 6, 12, 30, tzinfo=UTC),
        alerts_total=0,
        autofire_enabled=False,
    )

    app.state.ingest = ingest
    app.state.hub = hub
    app.state.strategies = strategies
    app.state.position_tracker = tracker
    app.state.missed_entry_detector = detector

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["per_resolution"]["15m"]["queue_depth"] == 0
    assert body["signals_fired_today"] == 7
    assert body["missed_entry_detector"]["running"] is True
    assert body["missed_entry_detector"]["alerts_total"] == 0


def test_feed_health_defaults_healthy_without_method() -> None:
    """A runner that predates the watchdog (no feed_health) reads healthy."""
    assert status_mod._feed_health(None) == {"feed_healthy": True}
    assert status_mod._feed_health(SimpleNamespace())["feed_healthy"] is True


def test_feed_health_passthrough_and_failure_safe() -> None:
    good = SimpleNamespace(feed_health=lambda: {"feed_healthy": False, "x": 1})
    assert status_mod._feed_health(good) == {"feed_healthy": False, "x": 1}

    def _boom() -> dict:
        raise RuntimeError("nope")

    bad = SimpleNamespace(feed_health=_boom)
    assert status_mod._feed_health(bad) == {"feed_healthy": True}
