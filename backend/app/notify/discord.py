from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.notify.base import AlertResult
from app.strategies.base import Signal

log = logging.getLogger("taiex.notify.discord")

_SIDE_COLOR = {"LONG": 0x2ECC71, "SHORT": 0xE74C3C, "EXIT": 0x95A5A6, "FLAT": 0x95A5A6}


class DiscordNotifier:
    name = "discord"

    def __init__(self, url: str | None = None) -> None:
        self._url = url if url is not None else get_settings().discord_webhook_url

    async def send(self, signal: Signal) -> AlertResult:
        if not self._url:
            return AlertResult(channel=self.name, ok=False, error="no webhook url configured")
        embed = {
            "title": f"{signal.strategy} → {signal.side}",
            "description": signal.reason or None,
            "color": _SIDE_COLOR.get(signal.side, 0x3498DB),
            "fields": [
                {"name": "Symbol", "value": signal.symbol, "inline": True},
                {"name": "Resolution", "value": signal.resolution, "inline": True},
                {"name": "Price", "value": f"{signal.price:.2f}", "inline": True},
                {"name": "Time", "value": signal.ts.isoformat(), "inline": False},
            ],
        }
        payload = {"embeds": [embed], "username": "TAIEX bot"}
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(self._url, json=payload)
                ok = 200 <= r.status_code < 300
                err = None if ok else r.text[:300]
                return AlertResult(channel=self.name, ok=ok, http_code=r.status_code, error=err)
        except httpx.HTTPError as e:
            return AlertResult(channel=self.name, ok=False, error=str(e))
