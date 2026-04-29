from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Signal as SignalRow
from app.db.models import StrategyConfig
from app.strategies.registry import all_strategies, discover

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyOut(BaseModel):
    name: str
    resolutions: list[str]
    params_schema: dict
    enabled: bool
    params: dict
    channels: list[str]


class EnableBody(BaseModel):
    enabled: bool


class ParamsBody(BaseModel):
    params: dict | None = None
    channels: list[str] | None = None


async def _config_for(name: str) -> dict:
    async with session_scope() as s:
        row = (
            await s.execute(select(StrategyConfig).where(StrategyConfig.name == name))
        ).scalar_one_or_none()
    if row is None:
        return {"enabled": False, "params": {}, "channels": ["discord", "n8n", "inapp"]}
    return {"enabled": row.enabled, "params": row.params, "channels": row.channels}


@router.get("")
async def list_strategies() -> list[StrategyOut]:
    discover()
    out: list[StrategyOut] = []
    for name, cls in all_strategies().items():
        cfg = await _config_for(name)
        out.append(
            StrategyOut(
                name=name,
                resolutions=list(cls.resolutions),
                params_schema=cls.params_schema.model_json_schema(),
                **cfg,
            )
        )
    return out


@router.post("/{name}/enable")
async def enable_strategy(name: str, body: EnableBody) -> dict:
    if name not in all_strategies():
        raise HTTPException(404, f"unknown strategy: {name}")
    async with session_scope() as s:
        stmt = pg_insert(StrategyConfig).values(name=name, enabled=body.enabled)
        stmt = stmt.on_conflict_do_update(
            index_elements=["name"], set_={"enabled": body.enabled}
        )
        await s.execute(stmt)
        await s.commit()
    return {"name": name, "enabled": body.enabled}


@router.patch("/{name}/params")
async def set_params(name: str, body: ParamsBody) -> dict:
    cls = all_strategies().get(name)
    if cls is None:
        raise HTTPException(404, f"unknown strategy: {name}")
    if body.params is not None:
        try:
            cls.params_schema(**body.params)
        except Exception as e:
            raise HTTPException(400, f"invalid params: {e}") from None
    set_clause: dict = {}
    if body.params is not None:
        set_clause["params"] = body.params
    if body.channels is not None:
        set_clause["channels"] = body.channels
    if not set_clause:
        return {"name": name}
    async with session_scope() as s:
        stmt = pg_insert(StrategyConfig).values(name=name, **set_clause)
        stmt = stmt.on_conflict_do_update(index_elements=["name"], set_=set_clause)
        await s.execute(stmt)
        await s.commit()
    return {"name": name, **set_clause}


@router.get("/{name}/state")
async def strategy_state(name: str) -> dict:
    cls = all_strategies().get(name)
    if cls is None:
        raise HTTPException(404, f"unknown strategy: {name}")
    symbol = get_settings().symbol_display
    state = cls.dump_state(symbol)
    return {"name": name, "symbol": symbol, "state": state}


@router.get("/{name}/signals")
async def recent_signals(name: str, limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(SignalRow)
                .where(SignalRow.strategy == name)
                .order_by(SignalRow.ts.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat(),
            "symbol": r.symbol,
            "resolution": r.resolution,
            "side": r.side,
            "price": r.price,
            "payload": r.payload,
        }
        for r in rows
    ]
