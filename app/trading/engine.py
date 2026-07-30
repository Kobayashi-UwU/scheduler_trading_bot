import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.parser import ParsedCycle, StrategySignal
from app.config import (
    AI_SELECTED_ACCOUNT,
    ALL_ACCOUNTS,
    PAPER_ACCOUNTS,
    REAL_ACCOUNTS,
    is_real_account,
    real_source_for,
    settings,
    start_balance_for,
)
from app.db.models import EquityPoint, Trade, TradeSkip, TradeStatus
from app.trading.sizing import size_real

log = logging.getLogger("engine")

# Re-exported for callers that historically imported these from the engine.
__all__ = [
    "AI_SELECTED_ACCOUNT",
    "ALL_ACCOUNTS",
    "PAPER_ACCOUNTS",
    "REAL_ACCOUNTS",
    "get_balance",
    "get_open_position",
    "get_recent_trades",
    "manage_open_positions",
    "process_cycle",
]


def _account_filter(session: Session, account: str):
    """Trades belonging to a given virtual account.

    Identity is the `account` column (backfilled by app/db/migrations.py); the
    legacy (is_shadow, strategy) pair is no longer consulted.
    """
    return select(Trade).where(Trade.account == account)


def get_balance(session: Session, account: str) -> float:
    trades = session.execute(_account_filter(session, account)).scalars().all()
    realized = sum(t.pnl for t in trades if t.pnl is not None)
    return start_balance_for(account) + realized


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


def _log_skip(
    session: Session,
    *,
    account: str,
    strategy: str,
    signal: StrategySignal,
    analysis_id: int | None,
    balance: float,
    reason: str,
    lots: float,
    risk_amount: float,
    risk_pct: float,
) -> None:
    session.add(
        TradeSkip(
            account=account,
            strategy=strategy,
            analysis_id=analysis_id,
            action=signal.action,
            reason=reason,
            balance=balance,
            entry_price=signal.entry,
            stop_loss=signal.stop_loss,
            intended_lots=lots,
            intended_risk=risk_amount,
            intended_risk_pct=risk_pct * 100,
        )
    )
    log.info("SKIP %s %s: %s", account, strategy, reason)


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

    if is_real_account(account):
        # Broker-shaped sizing: fixed lot steps, hard minimum, margin check.
        sizing = size_real(balance, signal.entry, signal.stop_loss)
        if not sizing.ok:
            _log_skip(
                session,
                account=account,
                strategy=strategy,
                signal=signal,
                analysis_id=analysis_id,
                balance=balance,
                reason=sizing.reason,
                lots=sizing.lots,
                risk_amount=sizing.risk_amount,
                risk_pct=sizing.risk_pct,
            )
            return None
        size = sizing.size
        risk_amount = sizing.risk_amount
        lots = sizing.lots
        margin_used = sizing.margin
        risk_pct = sizing.risk_pct * 100
    else:
        # Paper tier: continuous sizing, always hits the risk target exactly.
        risk_amount = balance * settings.risk_per_trade
        risk_distance = abs(signal.entry - signal.stop_loss)
        size = risk_amount / risk_distance if risk_distance > 0 else 0
        lots = None
        margin_used = None
        risk_pct = None

    trade = Trade(
        strategy=strategy,
        is_shadow=is_shadow,
        account=account,
        analysis_id=analysis_id,
        direction=direction,
        status=TradeStatus.OPEN.value,
        entry_price=signal.entry,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        size=size,
        risk_amount=risk_amount,
        lots=lots,
        margin_used=margin_used,
        risk_pct=risk_pct,
        confidence=signal.confidence,
        reasoning=signal.reasoning,
    )
    session.add(trade)
    session.flush()
    if lots is not None:
        log.info(
            "OPEN %s %s %s %.2f lot @ %.2f (SL %.2f TP %.2f) risk %.2f (%.1f%%) margin %.2f",
            account, strategy, direction, lots, signal.entry, signal.stop_loss,
            signal.take_profit, risk_amount, risk_pct, margin_used,
        )
    else:
        log.info("OPEN %s %s %s @ %.2f (SL %.2f TP %.2f)", account, strategy, direction,
                  signal.entry, signal.stop_loss, signal.take_profit)
    return trade


def _close_trade(session: Session, trade: Trade, exit_price: float, status: str) -> None:
    if trade.direction == "LONG":
        pnl = trade.size * (exit_price - trade.entry_price)
    else:
        pnl = trade.size * (trade.entry_price - exit_price)

    trade.exit_price = exit_price
    trade.pnl = pnl
    trade.r_multiple = pnl / trade.risk_amount if trade.risk_amount else 0
    trade.status = status
    trade.closed_at = dt.datetime.utcnow()
    log.info("CLOSE trade#%s %s %s pnl=%.2f", trade.id, trade.account or trade.strategy,
             status, pnl)


def manage_open_positions(session: Session, current_price: float) -> None:
    """Check every open trade (paper + real) against SL/TP using the latest price."""
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


def _apply_signal(
    session: Session,
    *,
    account: str,
    strategy: str,
    signal: StrategySignal | None,
    is_shadow: bool,
    current_price: float,
    analysis_id: int,
) -> None:
    """Apply one validated signal to one account."""
    if signal is None or not signal.ok:
        return

    existing = get_open_position(session, account)
    if signal.action == "CLOSE" and existing:
        _close_trade(session, existing, current_price, TradeStatus.CLOSED_MANUAL.value)
    elif signal.action in ("BUY", "SELL") and not existing:
        _open_trade(
            session,
            strategy=strategy,
            is_shadow=is_shadow,
            account=account,
            signal=signal,
            analysis_id=analysis_id,
        )


def process_cycle(session: Session, parsed: ParsedCycle, current_price: float, analysis_id: int) -> None:
    """Apply one AI analysis cycle across every account tier.

    Paper tier (unchanged): a shadow account per strategy, plus `ai_selected` for
    whichever strategy the AI picked. Real tier: the same signals re-sized for a
    $100 broker account, for the subset of accounts in REAL_ACCOUNTS.
    """
    live = parsed.live_signal

    for strategy, sig in parsed.evaluations.items():
        _apply_signal(
            session,
            account=strategy,
            strategy=strategy,
            signal=sig,
            is_shadow=True,
            current_price=current_price,
            analysis_id=analysis_id,
        )

    _apply_signal(
        session,
        account=AI_SELECTED_ACCOUNT,
        strategy=parsed.selected_strategy,
        signal=live,
        is_shadow=False,
        current_price=current_price,
        analysis_id=analysis_id,
    )

    # Real tier mirrors the same signals; sizing is what differs.
    for account in REAL_ACCOUNTS:
        source = real_source_for(account)
        if source == AI_SELECTED_ACCOUNT:
            signal, strategy = live, parsed.selected_strategy
        else:
            signal, strategy = parsed.evaluations.get(source), source
        _apply_signal(
            session,
            account=account,
            strategy=strategy,
            signal=signal,
            is_shadow=False,
            current_price=current_price,
            analysis_id=analysis_id,
        )

    session.commit()

    for account in ALL_ACCOUNTS:
        session.add(EquityPoint(account=account, balance=get_balance(session, account)))
    session.commit()
