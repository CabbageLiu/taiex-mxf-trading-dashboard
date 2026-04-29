from __future__ import annotations

import asyncio
import logging

from app.db.engine import session_scope
from app.db.models import Alert
from app.notify.base import AlertResult, Notifier
from app.notify.discord import DiscordNotifier
from app.notify.inapp import InAppNotifier
from app.notify.n8n import N8nNotifier
from app.strategies.base import Signal

log = logging.getLogger("taiex.notify.hub")


class NotifierHub:
    """Fan-out signals to all enabled channels concurrently. Each channel's
    success/failure is recorded independently so one bad webhook never kills
    the rest."""

    def __init__(self, notifiers: list[Notifier] | None = None) -> None:
        self.inapp = InAppNotifier()
        if notifiers is None:
            notifiers = [DiscordNotifier(), N8nNotifier(), self.inapp]
        self._notifiers: dict[str, Notifier] = {n.name: n for n in notifiers}
        # ensure inapp ref is the same instance even if user passed their own list
        if "inapp" in self._notifiers and isinstance(self._notifiers["inapp"], InAppNotifier):
            self.inapp = self._notifiers["inapp"]

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def dispatch(
        self,
        signal: Signal,
        signal_id: int | None = None,
        channels: list[str] | None = None,
    ) -> list[AlertResult]:
        targets = (
            [self._notifiers[c] for c in channels if c in self._notifiers]
            if channels
            else list(self._notifiers.values())
        )
        if not targets:
            return []
        results = await asyncio.gather(
            *[self._send(n, signal, signal_id) for n in targets], return_exceptions=False
        )
        await self._record(signal_id, results)
        return results

    async def _send(
        self, notifier: Notifier, signal: Signal, signal_id: int | None = None
    ) -> AlertResult:
        try:
            if isinstance(notifier, InAppNotifier):
                return await notifier.send(signal, signal_id=signal_id)
            return await notifier.send(signal)
        except Exception as e:
            log.exception("notifier %s raised", notifier.name)
            return AlertResult(channel=notifier.name, ok=False, error=str(e))

    async def _record(self, signal_id: int | None, results: list[AlertResult]) -> None:
        try:
            async with session_scope() as s:
                for r in results:
                    s.add(
                        Alert(
                            signal_id=signal_id,
                            channel=r.channel,
                            status="ok" if r.ok else "error",
                            http_code=r.http_code,
                            error=r.error,
                        )
                    )
                await s.commit()
        except Exception:
            log.exception("failed to record alerts; continuing")
