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
    analysis_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    direction: Mapped[str] = mapped_column(String)  # LONG/SHORT
    status: Mapped[str] = mapped_column(String, default=TradeStatus.OPEN.value)

    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    risk_amount: Mapped[float] = mapped_column(Float)

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
