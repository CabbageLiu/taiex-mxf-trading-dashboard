from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True, frozen=True)
class Tick:
    ts: datetime
    symbol: str
    price: float
    source: str


class MarketDataAdapter(Protocol):
    symbol: str
    source: str

    def stream_ticks(self) -> AsyncIterator[Tick]: ...

    async def backfill(self, start: datetime, end: datetime) -> list[Tick]: ...
