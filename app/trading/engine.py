import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.parser import ParsedCycle, StrategySignal
from app.config import STRATEGIES, settings
from app.db.models import EquityPoint, Trade, TradeStatus

log = logging.getLogger("engine")

AI_SELECTED_ACCOUNT = "ai_selected"
ALL_ACCOUNTS = [AI_SELECTED_ACCOUNT] + STRATEGIES


def _account_filter(session: Session, account: str):
    """Trades belonging to a given virtual account."""
    if account == AI_SELECTED_ACCOUNT:
        return select(Trade).where(Trade.is_shadow.is_(False))
    return select(Trade).where(Trade.is_shadow.is_(True), Trade.strategy == account)


def get_balance(session: Session, account: str) -> float:
    trades = session.execute(_account_filter(session, account)).scalars().all()
    realized = sum(t.pnl for t in trades if t.pnl is not None)
    return settings.start_balance + realized


def get_open_position(session: Session, account: str) -> Trade | None:
    stmt = _account_filter(session, account).where(Trade.status == TradeStatus.OPEN.value)
    return session.execute(stmt).scalars().first()


def get_recent_trades(session: Session, account: str, limit: int = 5) -> list[dict]:
    stmt = (
        _account_filter(session, account)
        .where(Trade.status != TradeStatus.OPEN.value)
        .order_by(Trade.closed_at.desc())
        .limit(limit)
    )
    trades = session.execute(stmt).scalars().all()
    return [
        {
            "direction": t.direction,
            "entry": t.entry_price,
            "exit": t.exit_price,
            "pnl": round(t.pnl, 2) if t.pnl is not None else None,
            "r_multiple": round(t.r_multiple, 2) if t.r_multiple is not None else None,
            "status": t.status,
        }
        for t in trades
    ]


def _open_trade(
    session: Session,
    *,
    strategy: str,
    is_shadow: bool,
    account: str,
    signal: StrategySignal,
    analysis_id: int | None,
) -> Trade | None:
    if get_open_position(session, account) is not None:
        return None  # max_open_positions=1 per account, already open

    direction = "LONG" if signal.action == "BUY" else "SHORT"
    balance = get_balance(session, account)
    risk_amount = balance * settings.risk_per_trade
    risk_distance = abs(signal.entry - signal.stop_loss)
    size = risk_amount / risk_distance if risk_distance > 0 else 0

    trade = Trade(
        strategy=strategy,
        is_shadow=is_shadow,
        analysis_id=analysis_id,
        direction=direction,
        status=TradeStatus.OPEN.value,
        entry_price=signal.entry,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        size=size,
        risk_amount=risk_amount,
        confidence=signal.confidence,
        reasoning=signal.reasoning,
    )
    session.add(trade)
    session.flush()
    log.info("OPEN %s %s %s @ %.2f (SL %.2f TP %.2f)", account, strategy, direction,
              signal.entry, signal.stop_loss, signal.take_profit)
    return trade


def _close_trade(session: Session, trade: Trade, exit_price: float, status: str) -> None:
    risk_distance = abs(trade.entry_price - trade.stop_loss)
    if trade.direction == "LONG":
        pnl = trade.size * (exit_price - trade.entry_price)
    else:
        pnl = trade.size * (trade.entry_price - exit_price)

    trade.exit_price = exit_price
    trade.pnl = pnl
    trade.r_multiple = pnl / trade.risk_amount if trade.risk_amount else 0
    trade.status = status
    trade.closed_at = dt.datetime.utcnow()
    log.info("CLOSE trade#%s %s %s pnl=%.2f", trade.id, trade.strategy, status, pnl)


def manage_open_positions(session: Session, current_price: float) -> None:
    """Check every open trade (real + shadow) against SL/TP using the latest price."""
    open_trades = session.execute(
        select(Trade).where(Trade.status == TradeStatus.OPEN.value)
    ).scalars().all()

    for trade in open_trades:
        hit_tp = (
            current_price >= trade.take_profit
            if trade.direction == "LONG"
            else current_price <= trade.take_profit
        )
        hit_sl = (
            current_price <= trade.stop_loss
            if trade.direction == "LONG"
            else current_price >= trade.stop_loss
        )
        if hit_sl:
            _close_trade(session, trade, trade.stop_loss, TradeStatus.CLOSED_SL.value)
        elif hit_tp:
            _close_trade(session, trade, trade.take_profit, TradeStatus.CLOSED_TP.value)

    session.commit()

    if open_trades:
        for account in ALL_ACCOUNTS:
            session.add(EquityPoint(account=account, balance=get_balance(session, account)))
        session.commit()


def process_cycle(session: Session, parsed: ParsedCycle, current_price: float, analysis_id: int) -> None:
    """Apply one AI analysis cycle: manage shadow accounts for every strategy, and the
    single ai_selected account for whichever strategy the AI picked as live."""

    for strategy, sig in parsed.evaluations.items():
        if not sig.ok:
            continue
        existing = get_open_position(session, strategy)
        if sig.action == "CLOSE" and existing:
            _close_trade(session, existing, current_price, TradeStatus.CLOSED_MANUAL.value)
        elif sig.action in ("BUY", "SELL") and not existing:
            _open_trade(
                session,
                strategy=strategy,
                is_shadow=True,
                account=strategy,
                signal=sig,
                analysis_id=analysis_id,
            )

    live = parsed.live_signal
    if live and live.ok:
        existing = get_open_position(session, AI_SELECTED_ACCOUNT)
        if live.action == "CLOSE" and existing:
            _close_trade(session, existing, current_price, TradeStatus.CLOSED_MANUAL.value)
        elif live.action in ("BUY", "SELL") and not existing:
            _open_trade(
                session,
                strategy=parsed.selected_strategy,
                is_shadow=False,
                account=AI_SELECTED_ACCOUNT,
                signal=live,
                analysis_id=analysis_id,
            )

    session.commit()

    for account in ALL_ACCOUNTS:
        session.add(EquityPoint(account=account, balance=get_balance(session, account)))
    session.commit()
