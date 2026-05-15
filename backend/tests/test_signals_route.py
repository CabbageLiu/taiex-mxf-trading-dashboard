"""GET /signals — unit tests.

Mocks the DB at the ``session_scope`` boundary and verifies the route's
filter / limit / serialization behaviour. No live DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.signals import router as signals_router


def _row(
    *,
    id_: int,
    ts: datetime,
    strategy: str = "always_long",
    side: str = "LONG",
    price: float | None = 20000.0,
    payload: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        ts=ts,
        symbol="MXF",
        resolution="1m",
        strategy=strategy,
        side=side,
        price=price,
        payload=payload or {},
    )


class _FakeScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeSession:
    """Filters in-memory rows based on the SQL stmt's WHERE clauses."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.last_stmt = None

    async def execute(self, stmt):
        self.last_stmt = stmt
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        rows = list(self._rows)
        # Filter: strategy = 'foo'
        import re

        m = re.search(r"signals\.strategy = '([^']*)'", compiled)
        if m:
            target = m.group(1)
            rows = [r for r in rows if r.strategy == target]
        # Filter: signals.ts >= ...
        m = re.search(r"signals\.ts >= '([^']+)'", compiled)
        if m:
            since = datetime.fromisoformat(m.group(1))
            rows = [r for r in rows if r.ts >= since]
        # Order by ts desc.
        rows.sort(key=lambda r: r.ts, reverse=True)
        # Honour LIMIT
        m = re.search(r"LIMIT (\d+)", compiled)
        if m:
            rows = rows[: int(m.group(1))]
        return _FakeResult(rows)


def _scope_factory(rows: list):
    @asynccontextmanager
    async def fake_scope():
        yield _FakeSession(rows)

    return fake_scope


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(signals_router)
    return a


async def _get(app: FastAPI, url: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(url)


async def test_list_signals_empty(app: FastAPI):
    with patch("app.api.routes.signals.session_scope", _scope_factory([])):
        r = await _get(app, "/signals")
    assert r.status_code == 200
    assert r.json() == []


async def test_list_signals_filter_strategy(app: FastAPI):
    t0 = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    rows = [
        _row(id_=1, ts=t0, strategy="alpha"),
        _row(id_=2, ts=t0, strategy="beta"),
    ]
    with patch("app.api.routes.signals.session_scope", _scope_factory(rows)):
        r = await _get(app, "/signals?strategy=alpha")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["strategy"] == "alpha"
    assert body[0]["id"] == 1


async def test_list_signals_since(app: FastAPI):
    from urllib.parse import quote

    t0 = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    t1 = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 29, 11, 0, tzinfo=UTC)
    rows = [
        _row(id_=1, ts=t0),
        _row(id_=2, ts=t1),
        _row(id_=3, ts=t2),
    ]
    cutoff = datetime(2026, 4, 29, 10, 30, tzinfo=UTC).isoformat()
    with patch("app.api.routes.signals.session_scope", _scope_factory(rows)):
        r = await _get(app, f"/signals?since={quote(cutoff, safe='')}")
    assert r.status_code == 200
    body = r.json()
    assert {b["id"] for b in body} == {3}


async def test_list_signals_limit(app: FastAPI):
    t0 = datetime(2026, 4, 29, 9, 0, tzinfo=UTC)
    from datetime import timedelta

    rows = [_row(id_=i, ts=t0 + timedelta(minutes=i)) for i in range(1, 6)]
    with patch("app.api.routes.signals.session_scope", _scope_factory(rows)):
        r = await _get(app, "/signals?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    # Most recent first.
    assert body[0]["id"] == 5
    assert body[1]["id"] == 4
