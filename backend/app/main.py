from contextlib import asynccontextmanager
from logging import getLogger

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import alerts, bars, indicators, strategies
from app.api.ws import router as ws_router
from app.db.engine import dispose_engine, init_engine
from app.ingest.runner import IngestRunner
from app.notify.hub import NotifierHub
from app.runner.strategy_loop import StrategyLoop

log = getLogger("taiex")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_engine()
    hub = NotifierHub()
    await hub.start()
    ingest = IngestRunner()
    await ingest.start()
    strat = StrategyLoop(hub=hub, ingest=ingest)
    await strat.start()

    app.state.ingest = ingest
    app.state.hub = hub
    app.state.strategies = strat

    try:
        yield
    finally:
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
    app.include_router(ws_router)

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


app = create_app()
