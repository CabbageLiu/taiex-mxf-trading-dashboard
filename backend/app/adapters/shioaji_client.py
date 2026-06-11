"""Shared Shioaji `api` singleton for live + historical access.

SinoPac caps connections at 5 / person ID and daily logins at 1000 / day,
so the live adapter and the historical client must share a single
``shioaji.Shioaji`` instance within one process.

Single-process assumption
-------------------------
This module enforces the connection cap only inside one Python process. If
the backend is ever run with ``uvicorn --workers N>1``, each worker creates
its own ``Shioaji`` instance and the cap is violated. The current
``docker-compose.yaml`` runs uvicorn single-worker; if that changes, ingest
must be pinned to one designated worker (or moved behind a separate
service that owns the SDK session).
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any

from app.config import get_settings

log = logging.getLogger("taiex.adapter.shioaji_client")

_lock = asyncio.Lock()
_api: Any | None = None
_logged_in: bool = False
_last_login_at: float = 0.0


async def get_api() -> Any:
    """Return the singleton ``shioaji.Shioaji`` instance, logging in if needed.

    The shioaji SDK login is synchronous + blocking; we run it in a thread.
    Successive callers re-use the same instance. A failed login marks the
    session unhealthy so the next caller retries (subject to cool-down).
    """
    global _api, _logged_in, _last_login_at

    async with _lock:
        if _api is not None and _logged_in:
            return _api

        cooldown = get_settings().shioaji_login_cooldown_sec
        wait = cooldown - (_time.monotonic() - _last_login_at)
        if wait > 0:
            log.info("shioaji login cool-down: sleeping %.1fs", wait)
            await asyncio.sleep(wait)

        if _api is None:
            import shioaji as sj  # type: ignore[import-not-found]

            settings = get_settings()
            _api = sj.Shioaji(simulation=settings.shioaji_simulation)

        await asyncio.to_thread(_do_login, _api)
        _logged_in = True
        _last_login_at = _time.monotonic()
        log.info("shioaji login successful (simulation=%s)", get_settings().shioaji_simulation)
        return _api


def _do_login(api: Any) -> None:
    settings = get_settings()
    if settings.shioaji_api_key is None or settings.shioaji_secret_key is None:
        raise RuntimeError("SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY not configured")
    api.login(
        api_key=settings.shioaji_api_key.get_secret_value(),
        secret_key=settings.shioaji_secret_key.get_secret_value(),
    )


def mark_session_broken() -> None:
    """Mark the cached session unhealthy so the next ``get_api()`` re-logs-in.

    Call this from adapter code when a subscribe / ticks call raises in a
    way that suggests an expired or closed session.
    """
    global _logged_in
    _logged_in = False


async def logout() -> None:
    """Release the SinoPac connection slot on app shutdown."""
    global _api, _logged_in
    async with _lock:
        if _api is None or not _logged_in:
            _api = None
            _logged_in = False
            return
        try:
            await asyncio.to_thread(_api.logout)
        except Exception:  # noqa: BLE001
            log.exception("shioaji logout raised; ignoring")
        _api = None
        _logged_in = False
        log.info("shioaji logout complete")


def _reset_for_tests() -> None:
    """Test hook: clear the module state between tests."""
    global _api, _logged_in, _last_login_at
    _api = None
    _logged_in = False
    _last_login_at = 0.0
