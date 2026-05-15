"""POST /admin/test-webhook — unit tests.

The route fans a synthetic ``Signal`` through ``NotifierHub.dispatch`` for a
single channel. We patch ``NotifierHub.dispatch`` (so no real HTTP traffic)
and ``get_settings`` (to control which channels are 'configured').
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.admin import router as admin_router
from app.notify.base import AlertResult


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(admin_router, prefix="/admin")
    return a


def _settings(*, discord: str = "", n8n: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        discord_webhook_url=discord,
        n8n_webhook_url=n8n,
        symbol_display="MXF",
    )


async def _post(app: FastAPI, url: str, json):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(url, json=json)


async def test_test_webhook_discord_unconfigured_503(app: FastAPI):
    with patch(
        "app.api.routes.admin.get_settings", lambda: _settings(discord="", n8n="")
    ):
        r = await _post(app, "/admin/test-webhook", {"channel": "discord"})
    assert r.status_code == 503
    assert "DISCORD_WEBHOOK_URL" in r.json()["detail"]


async def test_test_webhook_n8n_unconfigured_503(app: FastAPI):
    with patch(
        "app.api.routes.admin.get_settings", lambda: _settings(discord="", n8n="")
    ):
        r = await _post(app, "/admin/test-webhook", {"channel": "n8n"})
    assert r.status_code == 503
    assert "N8N_WEBHOOK_URL" in r.json()["detail"]


async def test_test_webhook_inapp_succeeds_via_synthetic_signal(app: FastAPI):
    # InAppNotifier needs no env. Patch dispatch to capture the signal +
    # the requested channel filter without firing the real notifier chain.
    fake_dispatch = AsyncMock(
        return_value=[AlertResult(channel="inapp", ok=True, http_code=None, error=None)]
    )
    with (
        patch("app.api.routes.admin.get_settings", lambda: _settings()),
        patch("app.api.routes.admin.NotifierHub.dispatch", fake_dispatch),
    ):
        r = await _post(app, "/admin/test-webhook", {"channel": "inapp"})
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "inapp"
    assert body["ok"] is True
    assert body["error"] is None
    fake_dispatch.assert_awaited_once()
    # Make sure the channel filter was honoured.
    kwargs = fake_dispatch.await_args.kwargs
    args = fake_dispatch.await_args.args
    channels = kwargs.get("channels") if "channels" in kwargs else args[2]
    assert channels == ["inapp"]


async def test_test_webhook_unknown_channel_validation_error(app: FastAPI):
    with patch("app.api.routes.admin.get_settings", lambda: _settings()):
        r = await _post(app, "/admin/test-webhook", {"channel": "fakecorp"})
    assert r.status_code == 422


async def test_test_webhook_failure_propagates_alertresult(app: FastAPI):
    fake_dispatch = AsyncMock(
        return_value=[
            AlertResult(channel="discord", ok=False, http_code=500, error="boom")
        ]
    )
    with (
        patch(
            "app.api.routes.admin.get_settings",
            lambda: _settings(discord="https://hooks.example/discord"),
        ),
        patch("app.api.routes.admin.NotifierHub.dispatch", fake_dispatch),
    ):
        r = await _post(app, "/admin/test-webhook", {"channel": "discord"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["http_code"] == 500
    assert body["error"] == "boom"
