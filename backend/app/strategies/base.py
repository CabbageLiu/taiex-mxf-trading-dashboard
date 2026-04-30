from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal

import pandas as pd
from pydantic import BaseModel

Side = Literal["LONG", "SHORT", "EXIT", "FLAT"]


@dataclass(slots=True)
class BarEvent:
    symbol: str
    resolution: str
    bucket: datetime
    bars: pd.DataFrame
    indicators: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass(slots=True)
class Signal:
    ts: datetime
    symbol: str
    resolution: str
    strategy: str
    side: Side
    price: float
    reason: str = ""
    payload: dict = field(default_factory=dict)


class EmptyParams(BaseModel):
    pass


class Strategy(ABC):
    name: ClassVar[str]
    display_name: ClassVar[str | None] = None
    resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = EmptyParams
    indicator_specs: ClassVar[dict[str, dict]] = {}

    def __init__(self, params: BaseModel | None = None) -> None:
        self.params = params or self.params_schema()

    @abstractmethod
    def on_bar(self, ev: BarEvent) -> Signal | None: ...

    @classmethod
    def dump_state(cls, symbol: str) -> dict:
        """Optional: return current strategy state for a given symbol.

        Strategies that hold module-level / shared state (e.g. open
        positions, daily confidence scores) override this to expose a
        snapshot via the /strategies/{name}/state endpoint. Default is
        an empty dict (stateless strategies).
        """
        return {}
