from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.routes.bars import VALID_RES

log = logging.getLogger("taiex.ws")

router = APIRouter()


@router.websocket("/ws/stream")
async def ws_stream(
    ws: WebSocket,
    res: str = Query(default="1m"),
) -> None:
    if res not in VALID_RES:
        await ws.close(code=1008, reason=f"invalid resolution {res}")
        return

    await ws.accept()
    app = ws.app
    ingest = app.state.ingest
    inapp = app.state.hub.inapp

    bar_q = ingest.subscribe(res)
    inapp_q = inapp.subscribe()

    async def pipe(q: asyncio.Queue, prefix: str | None = None) -> None:
        try:
            while True:
                msg = await q.get()
                if prefix:
                    msg = {**msg, "_prefix": prefix}
                await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            return

    bar_task = asyncio.create_task(pipe(bar_q))
    sig_task = asyncio.create_task(pipe(inapp_q))

    try:
        while True:
            # Discard client messages; we only push.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        bar_task.cancel()
        sig_task.cancel()
        ingest.unsubscribe(res, bar_q)
        inapp.unsubscribe(inapp_q)
        for t in (bar_task, sig_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
