from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.notify.base import AlertResult
from app.notify.hub import NotifierHub
from app.strategies.base import Signal


class _Recorder:
    def __init__(self, name: str, raises: bool = False, ok: bool = True):
        self.name = name
        self.raises = raises
        self.ok = ok
        self.calls: list[Signal] = []

    async def send(self, signal: Signal) -> AlertResult:
        self.calls.append(signal)
        if self.raises:
            raise RuntimeError(f"{self.name} blew up")
        return AlertResult(channel=self.name, ok=self.ok, http_code=200 if self.ok else 500)


@pytest.fixture
def signal() -> Signal:
    return Signal(
        ts=datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc),
        symbol="MXF",
        resolution="1m",
        strategy="t",
        side="LONG",
        price=40000.0,
    )


async def test_dispatch_calls_every_notifier(signal):
    a = _Recorder("a")
    b = _Recorder("b")
    hub = NotifierHub(notifiers=[a, b])
    with patch.object(hub, "_record", return_value=None):
        results = await hub.dispatch(signal)
    assert {r.channel for r in results} == {"a", "b"}
    assert all(r.ok for r in results)
    assert a.calls and b.calls


async def test_one_failure_does_not_block_others(signal):
    a = _Recorder("a", raises=True)
    b = _Recorder("b")
    hub = NotifierHub(notifiers=[a, b])
    with patch.object(hub, "_record", return_value=None):
        results = await hub.dispatch(signal)
    by_chan = {r.channel: r for r in results}
    assert by_chan["a"].ok is False
    assert "blew up" in (by_chan["a"].error or "")
    assert by_chan["b"].ok is True


async def test_channels_filter(signal):
    a = _Recorder("a")
    b = _Recorder("b")
    hub = NotifierHub(notifiers=[a, b])
    with patch.object(hub, "_record", return_value=None):
        results = await hub.dispatch(signal, channels=["b"])
    assert [r.channel for r in results] == ["b"]
    assert a.calls == []
    assert len(b.calls) == 1


async def test_notify_ops_routes_to_discord_only_no_db_record():
    """`notify_ops` must hit DiscordNotifier.notify_ops and NOT call dispatch's
    DB-recording path (no alerts/signals row, no in-app fan-out → no phantom
    trade)."""
    from unittest.mock import AsyncMock

    from app.notify.discord import DiscordNotifier

    discord = DiscordNotifier(url="https://discord.test/hook")
    discord.notify_ops = AsyncMock(return_value=AlertResult(channel="discord", ok=True))
    hub = NotifierHub(notifiers=[discord])
    with patch.object(hub, "_record") as record:
        await hub.notify_ops("ops message")
    discord.notify_ops.assert_awaited_once_with("ops message")
    record.assert_not_called()


async def test_notify_ops_without_discord_is_safe():
    """No Discord notifier configured → notify_ops is a silent no-op."""
    a = _Recorder("a")
    hub = NotifierHub(notifiers=[a])
    await hub.notify_ops("ops message")  # must not raise
    assert a.calls == []
