"""Tests for the optional ``display_name`` ClassVar on the Strategy ABC and
its surfacing through the ``GET /strategies`` route.

Mocks ``session_scope`` so no live DB is required.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import ClassVar
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.api.routes.strategies import router as strategies_router
from app.strategies.base import BarEvent, Strategy
from app.strategies.registry import _registry


class _StubParams(BaseModel):
    pass


class _StubNoDisplayName(Strategy):
    name: ClassVar[str] = "_test_stub_no_display"
    resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams

    def on_bar(self, ev: BarEvent):
        return None


class _StubWithDisplayName(Strategy):
    name: ClassVar[str] = "_test_stub_with_display"
    display_name: ClassVar[str | None] = "Pretty Display Name"
    resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams

    def on_bar(self, ev: BarEvent):
        return None


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(strategies_router)
    return a


@pytest.fixture
def isolated_registry():
    """Snapshot/restore _registry so we can install only our stubs for the
    list-route test (we do NOT want trade_strat_v1/v2 etc. spamming the
    response and possibly hitting other patches)."""
    saved = dict(_registry)
    _registry.clear()
    _registry[_StubNoDisplayName.name] = _StubNoDisplayName
    _registry[_StubWithDisplayName.name] = _StubWithDisplayName
    yield
    _registry.clear()
    _registry.update(saved)


def test_strategy_abc_default_display_name_is_none() -> None:
    """A strategy that does NOT declare display_name inherits None."""
    assert _StubNoDisplayName.display_name is None
    # The base ABC default itself:
    assert Strategy.display_name is None


def test_strategy_can_override_display_name() -> None:
    assert _StubWithDisplayName.display_name == "Pretty Display Name"


@pytest.mark.asyncio
async def test_strategies_route_returns_display_name_field(
    app: FastAPI, isolated_registry: None
) -> None:
    """Each item in GET /strategies has a display_name key (str or null)."""

    class _FakeSession:
        async def execute(self, stmt):  # noqa: ANN001
            class _R:
                def scalar_one_or_none(self_inner) -> None:
                    return None

            return _R()

    @asynccontextmanager
    async def _scope():
        yield _FakeSession()

    # Prevent discover() from re-importing the example strategies (which would
    # repopulate the registry we just pinned to two stubs).
    with (
        patch("app.api.routes.strategies.session_scope", _scope),
        patch("app.api.routes.strategies.discover", lambda: None),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/strategies")

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2
    for item in body:
        assert "display_name" in item
        # str-or-null typed
        assert item["display_name"] is None or isinstance(item["display_name"], str)

    by_name = {item["name"]: item for item in body}
    assert by_name[_StubNoDisplayName.name]["display_name"] is None
    assert by_name[_StubWithDisplayName.name]["display_name"] == "Pretty Display Name"
