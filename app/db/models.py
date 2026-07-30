import enum

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Direction(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_MANUAL = "CLOSED_MANUAL"


class Analysis(Base):
    """One AI analysis cycle: regime call, strategy selection, raw signal."""

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

    symbol: Mapped[str] = mapped_column(String)
    price_at_analysis: Mapped[float] = mapped_column(Float)

    regime: Mapped[str] = mapped_column(String)
    regime_reasoning: Mapped[str] = mapped_column(String, default="")
    selected_strategy: Mapped[str] = mapped_column(String)
    selection_reasoning: Mapped[str] = mapped_column(String, default="")

    action: Mapped[str] = mapped_column(String)  # BUY/SELL/HOLD/CLOSE
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reasoning: Mapped[str] = mapped_column(String, default="")

    prompt: Mapped[str] = mapped_column(String, default="")
    raw_response: Mapped[str] = mapped_column(String, default="")

    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str] = mapped_column(String, default="")

    trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
    closed_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    strategy: Mapped[str] = mapped_column(String)  # trend_following, etc.
    is_shadow: Mapped[bool] = mapped_column(Boolean, default=False)
    # Canonical account identity. Paper: "ai_selected" or a strategy name.
    # Real tier: "real_ai_selected" / "real_trend_following". Backfilled from
    # (is_shadow, strategy) for rows written before this column existed; the
    # (is_shadow, strategy) pair is no longer used for account filtering.
    account: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    analysis_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    direction: Mapped[str] = mapped_column(String)  # LONG/SHORT
    status: Mapped[str] = mapped_column(String, default=TradeStatus.OPEN.value)

    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    risk_amount: Mapped[float] = mapped_column(Float)

    # Real tier only: broker-shaped sizing. Paper trades leave these NULL.
    lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reasoning: Mapped[str] = mapped_column(String, default="")


class EquityPoint(Base):
    __tablename__ = "equity_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
    account: Mapped[str] = mapped_column(String)  # "ai_selected", or strategy name
    balance: Mapped[float] = mapped_column(Float)


class BrokerToken(Base):
    """cTrader Open API OAuth tokens, per real-tier account. Refreshed in place by
    app.trading.ctrader_tokens rather than kept in env vars, since the access
    token rotates roughly every 30 days.
    """

    __tablename__ = "broker_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String, unique=True, index=True)
    ctid_trader_account_id: Mapped[int] = mapped_column(Integer)
    access_token: Mapped[str] = mapped_column(String)
    refresh_token: Mapped[str] = mapped_column(String)
    expires_at: Mapped[DateTime] = mapped_column(DateTime)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TradeSkip(Base):
    """A signal a real account could not act on (risk cap, margin, lot limits).

    Paper accounts never skip for sizing reasons, so this is real-tier only. It
    exists so the dashboard can show what a $100 account had to pass over
    instead of the trade silently vanishing.
    """

    __tablename__ = "trade_skips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped[str] = mapped_column(String, index=True)
    strategy: Mapped[str] = mapped_column(String)
    analysis_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    action: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    balance: Mapped[float] = mapped_column(Float)

    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    intended_lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    intended_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    intended_risk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
