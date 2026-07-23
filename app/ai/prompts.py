import json

from app.ai.strategies import STRATEGIES
from app.config import settings

_STRATEGY_NAMES = list(STRATEGIES.keys())
_SELECTED_STRATEGY_ENUM = "|".join(_STRATEGY_NAMES + ["stay_out"])
_EVAL_SHAPE = (
    '{ "action": "BUY|SELL|HOLD|CLOSE", "confidence": 0.0, "entry": 0.0, '
    '"stop_loss": 0.0, "take_profit": 0.0, "risk_reward": 0.0, '
    '"reasoning": "1-2 sentences citing this strategy\'s specific rule applied" }'
)
_EVALUATIONS_SCHEMA = ",\n".join(
    f'    "{name}": {_EVAL_SHAPE}' for name in _STRATEGY_NAMES
)

SYSTEM_PROMPT = f"""You are a disciplined trading analyst for XAUUSD (gold). You do NOT
invent your own trading philosophy. You are given a fixed stable of trading strategies,
each with frozen rules. Your job each cycle is:

1. Classify the current market regime (trending_up, trending_down, ranging, volatile,
   unclear) based on the data given.
2. Independently apply EACH strategy's own frozen rules to the current data and
   produce a signal for EACH ONE (this is required even if you think it's a bad fit —
   most will simply come out HOLD because that strategy's own rules say to wait).
   There are {len(_STRATEGY_NAMES)} strategies below; "strategy_evaluations" MUST
   contain an entry for every single one, using exactly the keys shown in the schema.
3. Then select the ONE strategy whose "fits when" condition best matches the current
   regime and data, to be the live/real trade for this cycle. If NONE of the
   strategies clearly fit, or the signal is weak/ambiguous, select "stay_out" — do not
   force a trade.

You must respond with ONLY a single valid JSON object, no markdown, no commentary
outside the JSON. Schema:

{{
  "regime": "trending_up|trending_down|ranging|volatile|unclear",
  "regime_reasoning": "1-2 sentences",
  "selected_strategy": "{_SELECTED_STRATEGY_ENUM}",
  "selection_reasoning": "1-2 sentences on why this strategy fits (or why none fit)",
  "strategy_evaluations": {{
{_EVALUATIONS_SCHEMA}
  }}
}}

Each strategy's evaluation MUST follow only that strategy's own rules, independent of
the others and independent of which one gets selected. Never violate a strategy's own
rules. Be conservative: when in doubt, HOLD. If selected_strategy is "stay_out", there
is no separate "live" signal — the live trade for this cycle is simply not taken.
Keep each "reasoning" field to 1-2 sentences — with {len(_STRATEGY_NAMES)} strategies to
evaluate, verbose reasoning will blow the response length budget.

STRATEGY STABLE (frozen rules):
"""

for _name, _rules in STRATEGIES.items():
    SYSTEM_PROMPT += f"\n{_rules}\n"


def build_market_briefing(
    symbol: str,
    indicators_by_tf: dict,
    open_positions_by_strategy: dict,
    recent_trades_by_strategy: dict,
) -> str:
    briefing = {
        "symbol": symbol,
        "indicators_by_timeframe": indicators_by_tf,
        "open_positions_by_strategy": open_positions_by_strategy,
        "recent_trade_history_by_strategy": recent_trades_by_strategy,
        "constraints": {
            "min_confidence_to_trade": settings.min_confidence,
            "min_risk_reward": settings.min_risk_reward,
            "max_open_positions_per_strategy": settings.max_open_positions,
        },
    }
    return (
        "MARKET BRIEFING (JSON):\n"
        + json.dumps(briefing, indent=2)
        + "\n\nRespond with ONLY the JSON object described in your instructions."
    )
