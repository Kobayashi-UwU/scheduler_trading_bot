import logging

from app.ai.client import AIClientError, call_deepseek
from app.ai.parser import parse_and_validate
from app.ai.prompts import SYSTEM_PROMPT, build_market_briefing
from app.config import STRATEGIES, settings
from app.data.indicators import summarize
from app.data.yahoo import fetch_all_timeframes, is_market_open
from app.db.database import get_session
from app.db.models import Analysis
from app.trading.engine import (
    AI_SELECTED_ACCOUNT,
    ALL_ACCOUNTS,
    get_open_position,
    get_recent_trades,
    manage_open_positions,
    process_cycle,
)

log = logging.getLogger("cycle")


def run_price_check() -> None:
    """Fast job: mark open positions to market and fill SL/TP."""
    if not is_market_open():
        return
    candles = fetch_all_timeframes()
    df = candles.get("15m")
    if df is None or df.empty:
        log.warning("price check: no data")
        return
    current_price = float(df["close"].iloc[-1])

    session = get_session()
    try:
        manage_open_positions(session, current_price)
    finally:
        session.close()


def run_analysis_cycle() -> None:
    """Full job: fetch data, call the AI, validate, execute trades, log everything."""
    if not is_market_open():
        log.info("market closed, skipping analysis cycle")
        return

    candles = fetch_all_timeframes()
    if candles.get("15m") is None or candles["15m"].empty:
        log.error("no candle data available, skipping cycle")
        return

    current_price = float(candles["15m"]["close"].iloc[-1])
    indicators_by_tf = {tf: summarize(df) for tf, df in candles.items()}

    session = get_session()
    try:
        open_positions_by_strategy = {}
        recent_trades_by_strategy = {}
        for account in ALL_ACCOUNTS:
            pos = get_open_position(session, account)
            open_positions_by_strategy[account] = (
                {
                    "direction": pos.direction,
                    "entry": pos.entry_price,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                    "unrealized_pnl": round(
                        pos.size * (current_price - pos.entry_price)
                        if pos.direction == "LONG"
                        else pos.size * (pos.entry_price - current_price),
                        2,
                    ),
                }
                if pos
                else None
            )
            recent_trades_by_strategy[account] = get_recent_trades(session, account, limit=3)

        user_prompt = build_market_briefing(
            settings.symbol,
            indicators_by_tf,
            open_positions_by_strategy,
            recent_trades_by_strategy,
        )

        try:
            raw_response = call_deepseek(SYSTEM_PROMPT, user_prompt)
        except AIClientError as exc:
            log.error("AI call failed: %s", exc)
            analysis = Analysis(
                symbol=settings.symbol,
                price_at_analysis=current_price,
                regime="unclear",
                selected_strategy="stay_out",
                action="HOLD",
                prompt=user_prompt,
                raw_response=str(exc),
                accepted=False,
                reject_reason=f"AI call failed: {exc}",
            )
            session.add(analysis)
            session.commit()
            return

        parsed = parse_and_validate(raw_response)

        analysis = Analysis(
            symbol=settings.symbol,
            price_at_analysis=current_price,
            regime=parsed.regime or "unclear",
            regime_reasoning=parsed.regime_reasoning,
            selected_strategy=parsed.selected_strategy or "stay_out",
            selection_reasoning=parsed.selection_reasoning,
            action=(parsed.live_signal.action if parsed.live_signal else "HOLD"),
            confidence=(parsed.live_signal.confidence if parsed.live_signal else 0.0),
            reasoning=(parsed.live_signal.reasoning if parsed.live_signal else ""),
            prompt=user_prompt,
            raw_response=raw_response,
            accepted=parsed.ok,
            reject_reason=parsed.reject_reason,
        )
        session.add(analysis)
        session.flush()

        if parsed.ok:
            process_cycle(session, parsed, current_price, analysis.id)
        else:
            log.warning("analysis rejected: %s", parsed.reject_reason)
            session.commit()

        # fast SL/TP check right after opening, using the same price
        manage_open_positions(session, current_price)
    finally:
        session.close()
