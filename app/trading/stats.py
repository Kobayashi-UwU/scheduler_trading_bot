from sqlalchemy.orm import Session

from app.config import settings
from app.trading.engine import _account_filter, get_balance
from app.db.models import Trade, TradeStatus


def compute_stats(session: Session, account: str) -> dict:
    trades = (
        session.execute(
            _account_filter(session, account).where(Trade.status != TradeStatus.OPEN.value)
        )
        .scalars()
        .all()
    )

    closed = [t for t in trades if t.pnl is not None]
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
    expectancy = (sum(t.pnl for t in closed) / len(closed)) if closed else 0.0
    avg_r = (
        sum(t.r_multiple for t in closed if t.r_multiple is not None) / len(closed)
        if closed
        else 0.0
    )

    balance = get_balance(session, account)
    equity_curve = _equity_curve(closed)
    max_dd = _max_drawdown(equity_curve)

    return {
        "account": account,
        "balance": round(balance, 2),
        "return_pct": round((balance - settings.start_balance) / settings.start_balance * 100, 2),
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "expectancy": round(expectancy, 2),
        "avg_r_multiple": round(avg_r, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }


def _equity_curve(closed_trades: list[Trade]) -> list[float]:
    ordered = sorted(closed_trades, key=lambda t: t.closed_at or t.created_at)
    curve = [settings.start_balance]
    for t in ordered:
        curve.append(curve[-1] + t.pnl)
    return curve


def _max_drawdown(curve: list[float]) -> float:
    if len(curve) < 2:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        dd = (peak - value) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def compute_all_stats(session: Session) -> list[dict]:
    from app.trading.engine import ALL_ACCOUNTS

    return [compute_stats(session, account) for account in ALL_ACCOUNTS]
