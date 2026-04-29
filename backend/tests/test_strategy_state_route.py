from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.api.routes.strategies import router as strategies_router
from app.strategies.base import BarEvent, Strategy
from app.strategies.registry import _registry


class _StubParams(BaseModel):
    pass


class _StubStateful(Strategy):
    name: ClassVar[str] = "_test_stub_stateful"
    resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams

    def on_bar(self, ev: BarEvent):
        return None

    @classmethod
    def dump_state(cls, symbol: str) -> dict:
        return {"foo": "bar", "symbol": symbol}


class _StubStateless(Strategy):
    name: ClassVar[str] = "_test_stub_stateless"
    resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = _StubParams

    def on_bar(self, ev: BarEvent):
        return None


@pytest.fixture(autouse=True)
def register_stubs():
    _registry[_StubStateful.name] = _StubStateful
    _registry[_StubStateless.name] = _StubStateless
    yield
    _registry.pop(_StubStateful.name, None)
    _registry.pop(_StubStateless.name, None)


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(strategies_router)
    return a


@pytest.mark.asyncio
async def test_state_route_returns_dump_state_payload(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/strategies/{_StubStateful.name}/state")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == _StubStateful.name
    assert body["state"] == {"foo": "bar", "symbol": body["symbol"]}


@pytest.mark.asyncio
async def test_state_route_empty_for_stateless(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/strategies/{_StubStateless.name}/state")
    assert r.status_code == 200
    assert r.json()["state"] == {}


@pytest.mark.asyncio
async def test_state_route_404_unknown(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/strategies/__nope__/state")
    assert r.status_code == 404
