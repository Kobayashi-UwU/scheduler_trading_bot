from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.config import settings
from app.data.yahoo import fetch_candles, is_market_open
from app.db.database import get_session
from app.db.models import Analysis, Trade, TradeStatus
from app.trading.engine import ALL_ACCOUNTS, get_open_position
from app.trading.stats import compute_all_stats, compute_stats

router = APIRouter()


@router.get("/api/status")
def status():
    return {
        "symbol": settings.symbol,
        "market_open": is_market_open(),
        "analysis_interval_min": settings.analysis_interval_min,
        "accounts": ALL_ACCOUNTS,
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
    session = get_session()
    try:
        out = []
        for account in ALL_ACCOUNTS:
            pos = get_open_position(session, account)
            if pos:
                out.append(
                    {
                        "account": account,
                        "strategy": pos.strategy,
                        "direction": pos.direction,
                        "entry_price": pos.entry_price,
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                        "confidence": pos.confidence,
                        "reasoning": pos.reasoning,
                        "opened_at": pos.created_at,
                    }
                )
        return out
    finally:
        session.close()


@router.get("/api/trades")
def trades(account: str | None = Query(None), limit: int = Query(100, le=500)):
    session = get_session()
    try:
        stmt = select(Trade).where(Trade.status != TradeStatus.OPEN.value)
        if account and account != "ai_selected":
            stmt = stmt.where(Trade.is_shadow.is_(True), Trade.strategy == account)
        elif account == "ai_selected":
            stmt = stmt.where(Trade.is_shadow.is_(False))
        stmt = stmt.order_by(Trade.closed_at.desc()).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "id": t.id,
                "strategy": t.strategy,
                "is_shadow": t.is_shadow,
                "direction": t.direction,
                "status": t.status,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
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
