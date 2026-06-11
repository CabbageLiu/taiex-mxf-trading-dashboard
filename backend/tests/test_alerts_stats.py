"""GET /alerts/stats — unit tests.

Patches ``session_scope`` to return canned aggregate rows and verifies the
shape produced by ``alerts_stats``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.alerts import router as alerts_router


def _agg(channel: str, status: str, count: int, last_ts: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(channel=channel, status=status, count=count, last_ts=last_ts)


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


def _scope_factory(rows: list):
    @asynccontextmanager
    async def fake_scope():
        yield _FakeSession(rows)

    return fake_scope


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(alerts_router)
    return a


async def _get(app: FastAPI, url: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(url)


async def test_alerts_stats_empty(app: FastAPI):
    with patch("app.api.routes.alerts.session_scope", _scope_factory([])):
        r = await _get(app, "/alerts/stats")
    assert r.status_code == 200
    assert r.json() == {}


async def test_alerts_stats_aggregates_by_channel(app: FastAPI):
    t_old = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    t_new = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    rows = [
        # discord: 1 ok (most recent), 1 error (older)
        _agg("discord", "ok", 1, t_new),
        _agg("discord", "error", 1, t_old),
        # n8n: 1 ok
        _agg("n8n", "ok", 1, t_new),
    ]
    with patch("app.api.routes.alerts.session_scope", _scope_factory(rows)):
        r = await _get(app, "/alerts/stats")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"discord", "n8n"}
    assert body["discord"]["sent"] == 1
    assert body["discord"]["failed"] == 1
    # last_ts is the max across both statuses.
    assert body["discord"]["last_ts"] == t_new.isoformat()
    assert body["n8n"]["sent"] == 1
    assert body["n8n"]["failed"] == 0
    assert body["n8n"]["last_ts"] == t_new.isoformat()
