"""Tests for ``ShioajiFuturesAdapter``.

The real ``shioaji`` SDK may not be importable in CI, so we install a
minimal stub module into ``sys.modules`` before importing the adapter.
This lets us drive the callback bridge from a background thread without
hitting any network. The stub is scoped to this module's lifetime via a
module-level setup/teardown so it cannot leak into other test files.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from datetime import datetime
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

TPE = ZoneInfo("Asia/Taipei")

_PREV_SHIOAJI = None


def _install_shioaji_stub() -> None:
    global _PREV_SHIOAJI
    if "shioaji" in sys.modules and getattr(
        sys.modules["shioaji"], "_taiex_stub", False
    ):
        return
    _PREV_SHIOAJI = sys.modules.get("shioaji")
    mod = ModuleType("shioaji")
    mod._taiex_stub = True  # type: ignore[attr-defined]
    mod.Exchange = type("Exchange", (), {})  # type: ignore[attr-defined]
    mod.TickFOPv1 = type("TickFOPv1", (), {})  # type: ignore[attr-defined]
    mod.BidAskFOPv1 = type("BidAskFOPv1", (), {})  # type: ignore[attr-defined]

    quote_type = SimpleNamespace(Tick="Tick", BidAsk="BidAsk")
    mod.constant = SimpleNamespace(QuoteType=quote_type)  # type: ignore[attr-defined]
    sys.modules["shioaji"] = mod


def _restore_shioaji_stub() -> None:
    if _PREV_SHIOAJI is not None:
        sys.modules["shioaji"] = _PREV_SHIOAJI
    else:
        sys.modules.pop("shioaji", None)


_install_shioaji_stub()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_shioaji_stub():
    yield
    _restore_shioaji_stub()

from app.adapters import shioaji_client  # noqa: E402
from app.adapters.shioaji_taiex import SOURCE, ShioajiFuturesAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402

# ---------------------------------------------------------------------------
# Fake Shioaji API
# ---------------------------------------------------------------------------


class _FakeQuote:
    def __init__(self):
        self._on_event = None
        self.subscribe_calls = 0

    def subscribe(self, contract, quote_type=None):
        self.subscribe_calls += 1

    def on_event(self, fn):
        self._on_event = fn
        return fn


class _FakeApi:
    def __init__(self):
        self._on_tick = None
        self.quote = _FakeQuote()
        self.Contracts = SimpleNamespace(
            Futures=SimpleNamespace(TXF=SimpleNamespace(TXFR1=object()))
        )

    def on_tick_fop_v1(self):
        def decorator(fn):
            self._on_tick = fn
            return fn

        return decorator

    # ------------------------------------------------------------------
    # Test helpers (not on the real SDK)
    # ------------------------------------------------------------------
    def push_tick(self, ts: datetime | int, close: float) -> None:
        tick = SimpleNamespace(datetime=ts, close=close)
        if self._on_tick is not None:
            self._on_tick(None, tick)

    def fire_event(self, event_code: int) -> None:
        if self.quote._on_event is not None:
            self.quote._on_event(0, event_code, "", "")


@pytest.fixture
def fake_api(monkeypatch):
    shioaji_client._reset_for_tests()
    api = _FakeApi()

    async def _get_api():
        return api

    monkeypatch.setattr(shioaji_client, "get_api", _get_api)
    yield api
    shioaji_client._reset_for_tests()


@pytest.fixture
def adapter(fake_api):
    return ShioajiFuturesAdapter(display_symbol="MXF")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_bridge_yields_tick_in_order(adapter, fake_api):
    stream = adapter.stream_ticks()
    # Drive the lazy session setup by reading one tick after pushing it.
    asyncio.get_running_loop()

    # Prime the adapter (registers callbacks + subscribes).
    consumer_task = asyncio.create_task(stream.__anext__())
    # Give _ensure_session a tick to run.
    await asyncio.sleep(0)
    fake_api.push_tick(datetime(2026, 4, 29, 9, 0, 0, tzinfo=TPE), 39200.0)
    tick = await asyncio.wait_for(consumer_task, timeout=1.0)

    assert tick.symbol == "MXF"
    assert tick.source == SOURCE
    assert tick.price == 39200.0
    assert tick.ts == datetime(2026, 4, 29, 9, 0, 0, tzinfo=TPE)
    assert fake_api.quote.subscribe_calls == 1


@pytest.mark.asyncio
async def test_naive_datetime_normalized_to_taipei(adapter, fake_api):
    stream = adapter.stream_ticks()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    fake_api.push_tick(datetime(2026, 4, 29, 9, 5, 0), 39201.5)
    tick = await asyncio.wait_for(task, timeout=1.0)

    assert tick.ts.tzinfo is not None
    assert tick.ts == datetime(2026, 4, 29, 9, 5, 0, tzinfo=TPE)


@pytest.mark.asyncio
async def test_sub_floor_price_is_filtered(adapter, fake_api):
    stream = adapter.stream_ticks()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    fake_api.push_tick(datetime(2026, 4, 29, 9, 0, tzinfo=TPE), 500.0)  # dropped
    fake_api.push_tick(datetime(2026, 4, 29, 9, 1, tzinfo=TPE), 39200.0)
    tick = await asyncio.wait_for(task, timeout=1.0)
    assert tick.price == 39200.0


@pytest.mark.asyncio
async def test_queue_full_drops_oldest(monkeypatch, fake_api, caplog):
    monkeypatch.setattr(get_settings(), "shioaji_queue_maxsize", 3, raising=False)
    adapter = ShioajiFuturesAdapter(display_symbol="MXF")
    # Build queue manually with new max.
    adapter._queue = asyncio.Queue(maxsize=3)
    stream = adapter.stream_ticks()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)

    caplog.set_level(logging.WARNING, logger="taiex.adapter.shioaji")
    # Push 4 ticks without yielding to the consumer — must overflow the cap
    # of 3 before the consumer ever pulls. The drop-oldest path is what we
    # are testing.
    for sec, price in enumerate([39200, 39201, 39202, 39203]):
        fake_api.push_tick(
            datetime(2026, 4, 29, 9, 0, sec, tzinfo=TPE), float(price)
        )

    received = []
    received.append(await asyncio.wait_for(task, timeout=1.0))
    for _ in range(2):
        received.append(
            await asyncio.wait_for(stream.__anext__(), timeout=1.0)
        )

    prices = [t.price for t in received]
    # Oldest (39200) was dropped on overflow; consumer sees the latest 3.
    assert prices == [39201.0, 39202.0, 39203.0]
    assert any("queue full" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_reconnect_event_resubscribes_without_duplicating_callbacks(
    adapter, fake_api
):
    stream = adapter.stream_ticks()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    # Prime via one tick to ensure session setup ran.
    fake_api.push_tick(datetime(2026, 4, 29, 9, 0, 0, tzinfo=TPE), 39200.0)
    first = await asyncio.wait_for(task, timeout=1.0)
    assert first.price == 39200.0
    initial_subscribes = fake_api.quote.subscribe_calls

    # Fire reconnect event twice back-to-back.
    fake_api.fire_event(2)  # disconnect (no-op)
    fake_api.fire_event(4)  # reconnect → re-subscribe
    fake_api.fire_event(4)  # reconnect again

    # Wait for both re-subscribes to land (each goes through asyncio.to_thread
    # which schedules on the executor — needs a few loop iterations).
    async def wait_for_subscribes(target: int) -> None:
        while fake_api.quote.subscribe_calls < target:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(
        wait_for_subscribes(initial_subscribes + 2), timeout=1.0
    )
    assert fake_api.quote.subscribe_calls == initial_subscribes + 2

    # Feed 5 ticks; consumer must see exactly 5 (no doubling from duplicate cb).
    received: list[float] = []

    async def drain():
        async for tick in stream:
            received.append(tick.price)
            if len(received) >= 5:
                return

    drain_task = asyncio.create_task(drain())
    for sec in range(5):
        fake_api.push_tick(
            datetime(2026, 4, 29, 9, 0, sec, tzinfo=TPE), 40000.0 + sec
        )
        await asyncio.sleep(0)
    await asyncio.wait_for(drain_task, timeout=1.0)
    assert received == [40000.0, 40001.0, 40002.0, 40003.0, 40004.0]


@pytest.mark.asyncio
async def test_callback_from_real_background_thread(adapter, fake_api):
    """Verifies the call_soon_threadsafe boundary using a real OS thread."""
    stream = adapter.stream_ticks()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)

    def push_from_thread():
        fake_api.push_tick(
            datetime(2026, 4, 29, 9, 30, 0, tzinfo=TPE), 39250.0
        )

    t = threading.Thread(target=push_from_thread)
    t.start()
    t.join(timeout=1.0)

    tick = await asyncio.wait_for(task, timeout=1.0)
    assert tick.price == 39250.0


@pytest.mark.asyncio
async def test_backfill_method_returns_empty(adapter):
    out = await adapter.backfill(
        datetime(2026, 4, 28, tzinfo=TPE), datetime(2026, 4, 29, tzinfo=TPE)
    )
    assert out == []


@pytest.mark.asyncio
async def test_reconnect_relogins_resubscribes_and_resumes(
    adapter, fake_api, monkeypatch
):
    """Forced reconnect: logout + fresh get_api + re-subscribe, then ticks flow.

    Also exercises the deadlock fix — reconnect acquires _session_lock then
    drives _establish_locked under the SAME acquisition; if it re-acquired the
    non-reentrant lock this would hang and the wait_for would time out.
    """
    logout_calls = {"n": 0}

    async def _fake_logout():
        logout_calls["n"] += 1

    monkeypatch.setattr(shioaji_client, "logout", _fake_logout)

    stream = adapter.stream_ticks()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    fake_api.push_tick(datetime(2026, 4, 29, 9, 0, 0, tzinfo=TPE), 39200.0)
    first = await asyncio.wait_for(task, timeout=1.0)
    assert first.price == 39200.0

    gen_before = adapter._session_gen
    subs_before = fake_api.quote.subscribe_calls

    await asyncio.wait_for(adapter.reconnect(), timeout=1.0)

    assert logout_calls["n"] == 1
    # reconnect retires the old generation before teardown, then _establish_locked
    # bumps again for the fresh session → strictly advances past gen_before.
    assert adapter._session_gen > gen_before
    assert fake_api.quote.subscribe_calls == subs_before + 1

    # New session delivers ticks normally.
    task2 = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    fake_api.push_tick(datetime(2026, 4, 29, 9, 1, 0, tzinfo=TPE), 39300.0)
    second = await asyncio.wait_for(task2, timeout=1.0)
    assert second.price == 39300.0


@pytest.mark.asyncio
async def test_stale_callback_from_retired_generation_noops(
    adapter, fake_api, monkeypatch
):
    """A tick callback bound to the pre-reconnect generation must no-op so a
    zombie SDK thread cannot enqueue against the fresh session."""

    async def _fake_logout():
        return None

    monkeypatch.setattr(shioaji_client, "logout", _fake_logout)

    stream = adapter.stream_ticks()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    fake_api.push_tick(datetime(2026, 4, 29, 9, 0, 0, tzinfo=TPE), 39200.0)
    await asyncio.wait_for(task, timeout=1.0)

    # Capture the callback from the current (soon-to-be-retired) generation.
    stale_cb = fake_api._on_tick
    assert stale_cb is not None

    await asyncio.wait_for(adapter.reconnect(), timeout=1.0)

    # Invoking the stale callback must NOT enqueue anything.
    qsize_before = adapter._queue.qsize()
    stale_tick = SimpleNamespace(
        datetime=datetime(2026, 4, 29, 9, 0, 5, tzinfo=TPE), close=99999.0
    )
    stale_cb(None, stale_tick)
    await asyncio.sleep(0)
    assert adapter._queue.qsize() == qsize_before

    # The fresh-generation callback still works.
    task2 = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    fake_api.push_tick(datetime(2026, 4, 29, 9, 2, 0, tzinfo=TPE), 39400.0)
    tick = await asyncio.wait_for(task2, timeout=1.0)
    assert tick.price == 39400.0
