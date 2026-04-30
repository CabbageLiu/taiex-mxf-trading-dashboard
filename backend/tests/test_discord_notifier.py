"""Tests for the enriched DiscordNotifier embed (V5.2).

Asserts: display_name lookup with canonical-name fallback, entry_ind /
exit_ind / exit_reason / pnl_points / signal_id rendering, Asia/Taipei
timestamp formatting, and clean omission when payload fields are absent.

httpx.AsyncClient.post is patched with a stub that captures the JSON body
without hitting Discord.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.notify.discord import DiscordNotifier, _display_name_for, _fmt_ind
from app.strategies.base import Signal
from app.strategies.registry import discover


@pytest.fixture(scope="module", autouse=True)
def _ensure_strategies_registered() -> None:
    """Lazy-load the strategy registry once per test module so the
    ``_display_name_for`` lookup resolves real example strategies. Module
    scope keeps it idempotent without polluting other test modules.
    """
    discover()


class _Resp:
    def __init__(self, status_code: int = 204, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _StubClient:
    """Captures the last posted body. Substituted via monkeypatch."""

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    last_body: dict | None = None

    async def post(self, url: str, json: dict) -> _Resp:
        type(self).last_body = json
        return _Resp(204, "")


def _make_signal(side: str = "LONG", payload: dict | None = None) -> Signal:
    return Signal(
        ts=datetime(2026, 4, 30, 5, 30, 0, tzinfo=UTC),  # 13:30 CST
        symbol="MXF",
        resolution="30m",
        strategy="trade_strat_v1",
        side=side,
        price=17320.0,
        reason="entry conditions met",
        payload=payload or {},
    )


@pytest.fixture(autouse=True)
def _clear_lru() -> None:
    _display_name_for.cache_clear()


async def test_discord_embed_contains_display_name():
    n = DiscordNotifier(url="https://discord.example/webhook")
    sig = _make_signal()
    with patch("app.notify.discord.httpx.AsyncClient", _StubClient):
        result = await n.send(sig)
    assert result.ok
    body = _StubClient.last_body
    assert body is not None
    title = body["embeds"][0]["title"]
    # display_name for trade_strat_v1 = "30分鐘線策略"
    assert "30分鐘線策略" in title
    # canonical name appears in Strategy field when display differs
    fields = {f["name"]: f["value"] for f in body["embeds"][0]["fields"]}
    assert "Strategy" in fields
    assert "trade_strat_v1" in fields["Strategy"]


async def test_discord_embed_contains_entry_ind_when_present():
    n = DiscordNotifier(url="https://discord.example/webhook")
    sig = _make_signal(
        payload={
            "entry_ind": {
                "k": 54.3,
                "d": 51.2,
                "macd": 9.4,
                "signal": 7.1,
                "hist": 2.3,
                "plus_di": 33.0,
                "minus_di": 19.0,
                "adx": 27.0,
            }
        }
    )
    with patch("app.notify.discord.httpx.AsyncClient", _StubClient):
        await n.send(sig)
    fields = {f["name"]: f["value"] for f in _StubClient.last_body["embeds"][0]["fields"]}
    assert "開倉指標" in fields
    val = fields["開倉指標"]
    # Rounded to int
    assert "K54" in val and "D51" in val
    assert "MACD+9" in val
    assert "+DI33" in val and "-DI19" in val
    assert "ADX27" in val


async def test_discord_embed_omits_indicator_field_when_absent():
    n = DiscordNotifier(url="https://discord.example/webhook")
    sig = _make_signal(payload={})
    with patch("app.notify.discord.httpx.AsyncClient", _StubClient):
        await n.send(sig)
    field_names = {f["name"] for f in _StubClient.last_body["embeds"][0]["fields"]}
    assert "開倉指標" not in field_names
    assert "出場指標" not in field_names
    assert "出場原因" not in field_names
    assert "損益" not in field_names


async def test_discord_embed_close_signal_carries_exit_reason_and_pnl():
    n = DiscordNotifier(url="https://discord.example/webhook")
    sig = _make_signal(
        side="EXIT",
        payload={
            "exit_reason": "DI_FLIP_10M",
            "pnl_points": 87.5,
            "exit_ind": {
                "k": 60.0,
                "d": 55.0,
                "macd": 5.0,
                "signal": 6.0,
                "hist": -1.0,
                "plus_di": 18.0,
                "minus_di": 26.0,
                "adx": 24.0,
            },
        },
    )
    with patch("app.notify.discord.httpx.AsyncClient", _StubClient):
        await n.send(sig)
    fields = {f["name"]: f["value"] for f in _StubClient.last_body["embeds"][0]["fields"]}
    assert fields.get("出場原因") == "DI_FLIP_10M"
    assert fields.get("損益") == "+87.5 點"
    assert "出場指標" in fields


async def test_discord_embed_pnl_negative_renders_signed():
    n = DiscordNotifier(url="https://discord.example/webhook")
    sig = _make_signal(side="EXIT", payload={"exit_reason": "SL", "pnl_points": -60.0})
    with patch("app.notify.discord.httpx.AsyncClient", _StubClient):
        await n.send(sig)
    fields = {f["name"]: f["value"] for f in _StubClient.last_body["embeds"][0]["fields"]}
    assert fields.get("損益") == "-60.0 點"


async def test_discord_embed_time_is_taipei():
    n = DiscordNotifier(url="https://discord.example/webhook")
    sig = _make_signal()  # 05:30 UTC
    with patch("app.notify.discord.httpx.AsyncClient", _StubClient):
        await n.send(sig)
    fields = {f["name"]: f["value"] for f in _StubClient.last_body["embeds"][0]["fields"]}
    assert "Time" in fields
    # 05:30 UTC = 13:30 CST
    assert "13:30" in fields["Time"]
    assert "CST" in fields["Time"]


async def test_discord_embed_footer_carries_signal_id():
    n = DiscordNotifier(url="https://discord.example/webhook")
    sig = _make_signal()
    with patch("app.notify.discord.httpx.AsyncClient", _StubClient):
        await n.send(sig, signal_id=4242)
    embed = _StubClient.last_body["embeds"][0]
    assert embed.get("footer", {}).get("text") == "signal #4242"


async def test_discord_skips_post_when_no_url():
    # Explicit empty string bypasses the get_settings() fallback so this test
    # exercises the no-url branch independent of the test environment's .env.
    n = DiscordNotifier(url="")
    sig = _make_signal()
    result = await n.send(sig)
    assert result.ok is False
    assert "no webhook url" in (result.error or "")


async def test_fmt_ind_returns_none_for_all_null_snapshot():
    snap = {
        "k": None, "d": None, "macd": None, "signal": None,
        "hist": None, "plus_di": None, "minus_di": None, "adx": None,
    }
    assert _fmt_ind(snap) is None
    assert _fmt_ind(None) is None
    assert _fmt_ind({}) is None


async def test_display_name_unknown_strategy_falls_back_to_name():
    assert _display_name_for("__not_a_real_strategy__") == "__not_a_real_strategy__"


async def test_discord_send_handles_http_error():
    n = DiscordNotifier(url="https://discord.example/webhook")

    class _ErrClient(_StubClient):
        async def post(self, url: str, json: dict):
            raise httpx.HTTPError("boom")

    sig = _make_signal()
    with patch("app.notify.discord.httpx.AsyncClient", _ErrClient):
        result = await n.send(sig)
    assert result.ok is False
    assert "boom" in (result.error or "")
