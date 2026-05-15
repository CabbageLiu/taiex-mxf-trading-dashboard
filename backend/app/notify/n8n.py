from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.notify.base import AlertResult
from app.strategies.base import Signal

log = logging.getLogger("taiex.notify.n8n")


class N8nNotifier:
    name = "n8n"

    def __init__(self, url: str | None = None, secret: str | None = None) -> None:
        s = get_settings()
        self._url = url if url is not None else s.n8n_webhook_url
        self._secret = secret if secret is not None else s.alert_secret

    async def send(self, signal: Signal) -> AlertResult:
        if not self._url:
            return AlertResult(channel=self.name, ok=False, error="no webhook url configured")
        payload = {
            "alert_name": f"{signal.strategy}:{signal.side}",
            "symbol": signal.symbol,
            "value": signal.price,
            "side": signal.side,
            "strategy": signal.strategy,
            "resolution": signal.resolution,
            "data_time": signal.ts.isoformat(),
            "reason": signal.reason,
            "payload": signal.payload,
            "message": (
                f"{signal.strategy} {signal.side} on {signal.symbol} "
                f"@ {signal.price:.2f} ({signal.resolution})"
            ),
        }
        headers: dict[str, str] = {}
        if self._secret:
            headers["X-Alert-Secret"] = self._secret
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(self._url, json=payload, headers=headers)
                ok = 200 <= r.status_code < 300
                err = None if ok else r.text[:300]
                return AlertResult(channel=self.name, ok=ok, http_code=r.status_code, error=err)
        except httpx.HTTPError as e:
            return AlertResult(channel=self.name, ok=False, error=str(e))
