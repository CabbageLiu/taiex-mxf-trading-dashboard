"""Admin operations behind /admin (no auth gate yet — V4 single-user).

Currently exposes a synthetic-signal test for any single notifier channel
so the UI can verify webhook plumbing without waiting for a live signal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.notify.hub import NotifierHub
from app.strategies.base import Signal

router = APIRouter()


class TestWebhookRequest(BaseModel):
    channel: Literal["discord", "n8n", "inapp"]


class TestWebhookResponse(BaseModel):
    channel: str
    ok: bool
    http_code: int | None
    error: str | None


@router.post("/test-webhook", response_model=TestWebhookResponse)
async def post_test_webhook(body: TestWebhookRequest) -> TestWebhookResponse:
    settings = get_settings()
    if body.channel == "discord" and not settings.discord_webhook_url:
        raise HTTPException(503, "DISCORD_WEBHOOK_URL not configured")
    if body.channel == "n8n" and not settings.n8n_webhook_url:
        raise HTTPException(503, "N8N_WEBHOOK_URL not configured")

    hub = NotifierHub()
    synthetic = Signal(
        ts=datetime.utcnow(),
        symbol=settings.symbol_display,
        resolution="1m",
        strategy="__test__",
        side="LONG",
        price=0.0,
        reason="test webhook from /admin/test-webhook",
        payload={"test": True},
    )
    results = await hub.dispatch(synthetic, signal_id=None, channels=[body.channel])
    if not results:
        raise HTTPException(503, f"channel '{body.channel}' is not registered")
    r = results[0]
    return TestWebhookResponse(
        channel=r.channel,
        ok=r.ok,
        http_code=r.http_code,
        error=r.error,
    )
