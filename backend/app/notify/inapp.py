from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator

from app.notify.base import AlertResult
from app.strategies.base import Signal

log = logging.getLogger("taiex.notify.inapp")


class InAppNotifier:
    name = "inapp"

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict]] = set()

    async def send(self, signal: Signal, signal_id: int | None = None) -> AlertResult:
        msg = {
            "type": "signal",
            "id": signal_id,
            "ts": signal.ts.isoformat(),
            "symbol": signal.symbol,
            "resolution": signal.resolution,
            "strategy": signal.strategy,
            "side": signal.side,
            "price": signal.price,
            "reason": signal.reason,
            "payload": signal.payload,
        }
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("inapp subscriber queue full; dropping")
        return AlertResult(channel=self.name, ok=True)

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=1024)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def stream(self) -> AsyncIterator[dict]:
        q = self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(q)


# default registry-keyed map for hub fan-out
_subs: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)
