import asyncio
from contextlib import asynccontextmanager
from logging import getLogger

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    alerts,
    backfill,
    bars,
    indicators,
    insights,
    status,
    strategies,
    trades,
)
from app.api.ws import router as ws_router
from app.config import get_settings
from app.db.engine import dispose_engine, init_engine
from app.ingest.backfill import BackfillService
from app.ingest.runner import IngestRunner
from app.notify.hub import NotifierHub
from app.runner.position_tracker import PositionTracker
from app.runner.strategy_loop import StrategyLoop

log = getLogger("taiex")


async def _startup_backfill(days: int) -> None:
    """Fire-and-forget catch-up of any market days the server missed."""
    if days <= 0:
        return
    try:
        service = BackfillService()
        results = await service.backfill_recent(days)
        if results:
            inserted = sum(r.inserted for r in results)
            log.info("startup backfill: %d days, %d ticks inserted", len(results), inserted)
    except Exception:
        log.exception("startup backfill failed; continuing without it")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_engine()
    hub = NotifierHub()
    await hub.start()
    ingest = IngestRunner()
    await ingest.start()
    strat = StrategyLoop(hub=hub, ingest=ingest)
    await strat.start()
    tracker = PositionTracker(hub=hub)
    await tracker.start()

    settings = get_settings()
    backfill_task = asyncio.create_task(
        _startup_backfill(settings.backfill_on_startup_days),
        name="startup-backfill",
    )

    app.state.ingest = ingest
    app.state.hub = hub
    app.state.strategies = strat
    app.state.position_tracker = tracker
    app.state.backfill_task = backfill_task

    try:
        yield
    finally:
        backfill_task.cancel()
        try:
            await backfill_task
        except (asyncio.CancelledError, Exception):
            pass
        await tracker.stop()
        await strat.stop()
        await ingest.stop()
        await hub.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="TAIEX MXF Dashboard", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(bars.router)
    app.include_router(indicators.router)
    app.include_router(strategies.router)
    app.include_router(alerts.router)
    app.include_router(trades.router, prefix="/trades", tags=["trades"])
    app.include_router(status.router, tags=["status"])
    app.include_router(insights.router, prefix="/insights", tags=["insights"])
    app.include_router(backfill.router, prefix="/admin", tags=["admin"])
    app.include_router(ws_router)

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


app = create_app()
