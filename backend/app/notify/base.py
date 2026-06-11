from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.strategies.base import Signal


@dataclass(slots=True)
class AlertResult:
    channel: str
    ok: bool
    http_code: int | None = None
    error: str | None = None


class Notifier(Protocol):
    name: str

    async def send(self, signal: Signal) -> AlertResult: ...
