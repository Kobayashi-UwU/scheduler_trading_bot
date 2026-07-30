from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.config import ALL_ACCOUNTS, PAPER_ACCOUNTS, REAL_ACCOUNTS, settings
from app.data.yahoo import fetch_candles, is_market_open
from app.db.database import get_session
from app.db.models import Analysis, Trade, TradeSkip, TradeStatus
from app.trading.engine import get_open_position
from app.trading.stats import compute_all_stats, compute_real_stats, compute_stats

router = APIRouter()


def _position_payload(account: str, pos: Trade) -> dict:
    return {
        "id": pos.id,
        "account": account,
        "strategy": pos.strategy,
        "direction": pos.direction,
        "entry_price": pos.entry_price,
        "stop_loss": pos.stop_loss,
        "take_profit": pos.take_profit,
        "size": pos.size,
        "lots": pos.lots,
        "risk_amount": pos.risk_amount,
        "risk_pct": pos.risk_pct,
        "margin_used": pos.margin_used,
        "confidence": pos.confidence,
        "reasoning": pos.reasoning,
        "opened_at": pos.created_at,
    }


@router.get("/api/status")
def status():
    return {
        "symbol": settings.symbol,
        "market_open": is_market_open(),
        "analysis_interval_min": settings.analysis_interval_min,
        "accounts": PAPER_ACCOUNTS,
        "real_accounts": REAL_ACCOUNTS,
        "real_enabled": settings.real_enabled and bool(REAL_ACCOUNTS),
        "real_config": {
            "start_balance": settings.real_start_balance,
            "leverage": settings.real_leverage,
            "min_lot": settings.real_min_lot,
            "lot_step": settings.real_lot_step,
            "contract_size": settings.real_contract_size,
            "risk_per_trade_pct": settings.real_risk_per_trade * 100,
            "max_risk_pct": settings.real_max_risk_pct * 100,
        },
    }


@router.get("/api/stats")
def stats():
    session = get_session()
    try:
        return compute_all_stats(session)
    finally:
        session.close()


@router.get("/api/stats/{account}")
def stats_for_account(account: str):
    if account not in ALL_ACCOUNTS:
        raise HTTPException(status_code=404, detail="unknown account")
    session = get_session()
    try:
        return compute_stats(session, account)
    finally:
        session.close()


@router.get("/api/positions")
def positions():
    """Paper-tier open positions (the original endpoint)."""
    session = get_session()
    try:
        return [
            _position_payload(account, pos)
            for account in PAPER_ACCOUNTS
            if (pos := get_open_position(session, account))
        ]
    finally:
        session.close()


@router.get("/api/real/stats")
def real_stats():
    session = get_session()
    try:
        return compute_real_stats(session)
    finally:
        session.close()


@router.get("/api/real/positions")
def real_positions():
    session = get_session()
    try:
        return [
            _position_payload(account, pos)
            for account in REAL_ACCOUNTS
            if (pos := get_open_position(session, account))
        ]
    finally:
        session.close()


@router.get("/api/executor/status")
def executor_status():
    """Health of the MT5 mirror (last sync, last action, errors per account)."""
    from app.trading.executor import STATUS

    return {"enabled": settings.executor_enabled, **STATUS}


@router.get("/api/real/skips")
def real_skips(limit: int = Query(50, le=200)):
    """Signals the real accounts could not act on (risk cap, margin, lot limits)."""
    session = get_session()
    try:
        stmt = select(TradeSkip).order_by(TradeSkip.created_at.desc()).limit(limit)
        return [
            {
                "id": s.id,
                "created_at": s.created_at,
                "account": s.account,
                "strategy": s.strategy,
                "action": s.action,
                "reason": s.reason,
                "balance": s.balance,
                "entry_price": s.entry_price,
                "stop_loss": s.stop_loss,
                "intended_lots": s.intended_lots,
                "intended_risk": s.intended_risk,
                "intended_risk_pct": s.intended_risk_pct,
            }
            for s in session.execute(stmt).scalars().all()
        ]
    finally:
        session.close()


@router.get("/api/trades")
def trades(account: str | None = Query(None), limit: int = Query(100, le=500)):
    session = get_session()
    try:
        stmt = select(Trade).where(Trade.status != TradeStatus.OPEN.value)
        if account:
            if account not in ALL_ACCOUNTS:
                raise HTTPException(status_code=404, detail="unknown account")
            stmt = stmt.where(Trade.account == account)
        else:
            # Default view stays the paper experiment.
            stmt = stmt.where(Trade.account.in_(PAPER_ACCOUNTS))
        stmt = stmt.order_by(Trade.closed_at.desc()).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "id": t.id,
                "account": t.account,
                "strategy": t.strategy,
                "is_shadow": t.is_shadow,
                "direction": t.direction,
                "status": t.status,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "size": t.size,
                "lots": t.lots,
                "risk_amount": t.risk_amount,
                "risk_pct": t.risk_pct,
                "margin_used": t.margin_used,
                "pnl": t.pnl,
                "r_multiple": t.r_multiple,
                "confidence": t.confidence,
                "reasoning": t.reasoning,
                "created_at": t.created_at,
                "closed_at": t.closed_at,
            }
            for t in rows
        ]
    finally:
        session.close()


@router.get("/api/analyses")
def analyses(limit: int = Query(50, le=200)):
    session = get_session()
    try:
        stmt = select(Analysis).order_by(Analysis.created_at.desc()).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "id": a.id,
                "created_at": a.created_at,
                "price_at_analysis": a.price_at_analysis,
                "regime": a.regime,
                "regime_reasoning": a.regime_reasoning,
                "selected_strategy": a.selected_strategy,
                "selection_reasoning": a.selection_reasoning,
                "action": a.action,
                "confidence": a.confidence,
                "reasoning": a.reasoning,
                "accepted": a.accepted,
                "reject_reason": a.reject_reason,
            }
            for a in rows
        ]
    finally:
        session.close()


@router.get("/api/analyses/{analysis_id}")
def analysis_detail(analysis_id: int):
    session = get_session()
    try:
        a = session.get(Analysis, analysis_id)
        if not a:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "id": a.id,
            "created_at": a.created_at,
            "prompt": a.prompt,
            "raw_response": a.raw_response,
            "regime": a.regime,
            "selected_strategy": a.selected_strategy,
            "accepted": a.accepted,
            "reject_reason": a.reject_reason,
        }
    finally:
        session.close()


@router.get("/api/candles")
def candles(timeframe: str = Query("1h")):
    df = fetch_candles(timeframe)
    if df.empty:
        return []
    df = df.tail(200).reset_index()
    time_col = df.columns[0]
    return [
        {
            "time": row[time_col].isoformat(),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
        }
        for _, row in df.iterrows()
    ]
