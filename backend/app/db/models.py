from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tick(Base):
    __tablename__ = "ticks"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    resolution: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False, index=True)
    side: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


Index("ix_signals_strategy_ts", Signal.strategy, Signal.ts.desc())


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    signal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("signals.id"))
    channel: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    http_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String)


class StrategyConfig(Base):
    __tablename__ = "strategy_config"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, server_default="{discord,n8n,inapp}"
    )


class Trade(Base):
    __tablename__ = "trades"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)  # "LONG" | "SHORT"
    entry_ts = Column(DateTime(timezone=True), nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    entry_signal_id = Column(BigInteger, ForeignKey("signals.id"), nullable=True)
    exit_ts = Column(DateTime(timezone=True), nullable=True, index=True)
    exit_price = Column(Float, nullable=True)
    exit_signal_id = Column(BigInteger, ForeignKey("signals.id"), nullable=True)
    qty = Column(Float, nullable=False, default=1.0)
    pnl_points = Column(Float, nullable=True)  # NULL while open
    payload = Column(JSONB, nullable=False, default=dict)


Index("ix_trades_strategy_entry_ts", Trade.strategy, Trade.entry_ts.desc())
Index("ix_trades_symbol_entry_ts", Trade.symbol, Trade.entry_ts.desc())


class Trend(Base):
    __tablename__ = "trends"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    resolution: Mapped[str] = mapped_column(String, nullable=False)
    ema20: Mapped[float] = mapped_column(Float, nullable=False)
    ema50: Mapped[float] = mapped_column(Float, nullable=False)
    plus_di: Mapped[float] = mapped_column(Float, nullable=False)
    minus_di: Mapped[float] = mapped_column(Float, nullable=False)
    adx: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)


Index("ix_trends_symbol_ts_desc", Trend.symbol, Trend.ts.desc())
