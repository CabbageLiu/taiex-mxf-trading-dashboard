from __future__ import annotations

import logging
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.notify.base import AlertResult
from app.strategies.base import Signal

log = logging.getLogger("taiex.notify.discord")

_SIDE_COLOR = {"LONG": 0x2ECC71, "SHORT": 0xE74C3C, "EXIT": 0x95A5A6, "FLAT": 0x95A5A6}
_TAIPEI = ZoneInfo("Asia/Taipei")


@lru_cache(maxsize=64)
def _display_name_for(name: str) -> str:
    """Look up a registered strategy's display_name with cls.name fallback.

    Cached because every signal triggers a lookup. Cache size is per-process
    and bounded; strategy registry is effectively static after startup.
    """
    from app.strategies.registry import get as _get

    cls = _get(name)
    if cls is None:
        return name
    return getattr(cls, "display_name", None) or cls.name


def _fmt_num(v: float | int | None, *, signed: bool = False) -> str:
    if v is None:
        return "—"
    iv = round(float(v))
    if signed and iv >= 0:
        return f"+{iv}"
    return str(iv)


def _fmt_ind(snapshot: dict | None) -> str | None:
    """Format an 8-key indicator snapshot into a single-line string.

    Returns None when snapshot is empty / all-fields-None so the caller can
    skip emitting the field cleanly.
    """
    if not snapshot:
        return None
    keys = ("k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx")
    if all(snapshot.get(k) is None for k in keys):
        return None
    k = _fmt_num(snapshot.get("k"))
    d = _fmt_num(snapshot.get("d"))
    macd = _fmt_num(snapshot.get("macd"), signed=True)
    sig = _fmt_num(snapshot.get("signal"), signed=True)
    hist = _fmt_num(snapshot.get("hist"), signed=True)
    plus = _fmt_num(snapshot.get("plus_di"))
    minus = _fmt_num(snapshot.get("minus_di"))
    adx = _fmt_num(snapshot.get("adx"))
    return f"K{k} D{d}  MACD{macd} sig{sig} hist{hist}  +DI{plus} -DI{minus} ADX{adx}"


class DiscordNotifier:
    name = "discord"

    def __init__(self, url: str | None = None) -> None:
        self._url = url if url is not None else get_settings().discord_webhook_url

    async def send(self, signal: Signal, signal_id: int | None = None) -> AlertResult:
        if not self._url:
            return AlertResult(channel=self.name, ok=False, error="no webhook url configured")

        display = _display_name_for(signal.strategy)
        title = f"{display} → {signal.side}"
        ts_local = signal.ts.astimezone(_TAIPEI).strftime("%Y-%m-%d %H:%M:%S CST")

        fields: list[dict] = [
            {"name": "Symbol", "value": signal.symbol, "inline": True},
            {"name": "Resolution", "value": signal.resolution, "inline": True},
            {"name": "Price", "value": f"{signal.price:.2f}", "inline": True},
            {"name": "Time", "value": ts_local, "inline": False},
        ]

        if display != signal.strategy:
            strategy_value = f"{display} ({signal.strategy})"
        else:
            strategy_value = signal.strategy
        fields.append({"name": "Strategy", "value": strategy_value, "inline": False})

        payload = signal.payload or {}

        entry_line = _fmt_ind(payload.get("entry_ind"))
        if entry_line:
            fields.append({"name": "開倉指標", "value": f"```\n{entry_line}\n```", "inline": False})

        exit_line = _fmt_ind(payload.get("exit_ind"))
        if exit_line:
            fields.append({"name": "出場指標", "value": f"```\n{exit_line}\n```", "inline": False})

        exit_reason = payload.get("exit_reason")
        if exit_reason:
            fields.append({"name": "出場原因", "value": str(exit_reason), "inline": True})

        pnl = payload.get("pnl_points")
        if pnl is not None:
            fields.append({"name": "損益", "value": f"{float(pnl):+.1f} 點", "inline": True})

        embed: dict = {
            "title": title,
            "description": signal.reason or None,
            "color": _SIDE_COLOR.get(signal.side, 0x3498DB),
            "fields": fields,
        }
        if signal_id is not None:
            embed["footer"] = {"text": f"signal #{signal_id}"}

        body = {"embeds": [embed], "username": "TAIEX bot"}
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(self._url, json=body)
                ok = 200 <= r.status_code < 300
                err = None if ok else r.text[:300]
                return AlertResult(channel=self.name, ok=ok, http_code=r.status_code, error=err)
        except httpx.HTTPError as e:
            return AlertResult(channel=self.name, ok=False, error=str(e))
