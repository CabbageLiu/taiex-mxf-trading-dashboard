from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from app.indicators.atr import ATR
from app.indicators.base import Indicator
from app.indicators.candle import CandleDirection
from app.indicators.dmi import DMI
from app.indicators.kd import KD
from app.indicators.ma import MA
from app.indicators.macd import MACD
from app.indicators.rsi import RSI

_REGISTRY: dict[str, type[Indicator]] = {
    "ma": MA,
    "macd": MACD,
    "rsi": RSI,
    "kd": KD,
    "dmi": DMI,
    "atr": ATR,
    "candle_direction": CandleDirection,
}


def available() -> list[str]:
    return list(_REGISTRY.keys())


def compute(kind: str, bars: pd.DataFrame, params: dict[str, Any] | None = None) -> pd.DataFrame:
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise KeyError(f"unknown indicator: {kind}")
    return cls().compute(bars, params or {})


class IndicatorCache:
    """Tiny last-bar-keyed cache. Recomputes when the latest bar moves."""

    def __init__(self) -> None:
        self._cache: dict[tuple, tuple[datetime, pd.DataFrame]] = {}

    def get(
        self,
        symbol: str,
        resolution: str,
        kind: str,
        params: dict[str, Any],
        bars: pd.DataFrame,
    ) -> pd.DataFrame:
        if bars.empty:
            return pd.DataFrame()
        latest_idx = bars.index[-1]
        key = (symbol, resolution, kind, tuple(sorted(params.items())))
        cached = self._cache.get(key)
        if cached is not None and cached[0] == latest_idx:
            return cached[1]
        result = compute(kind, bars, params)
        self._cache[key] = (latest_idx, result)
        return result


cache = IndicatorCache()
