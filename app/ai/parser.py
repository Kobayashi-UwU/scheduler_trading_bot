import json
import re
from dataclasses import dataclass, field

from app.config import ALL_SELECTIONS, STRATEGIES, settings

VALID_REGIMES = {"trending_up", "trending_down", "ranging", "volatile", "unclear"}
VALID_ACTIONS = {"BUY", "SELL", "HOLD", "CLOSE"}


@dataclass
class StrategySignal:
    ok: bool
    strategy: str
    reject_reason: str = ""
    action: str = "HOLD"
    confidence: float = 0.0
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    reasoning: str = ""


@dataclass
class ParsedCycle:
    ok: bool
    reject_reason: str = ""
    raw: dict = field(default_factory=dict)
    regime: str = ""
    regime_reasoning: str = ""
    selected_strategy: str = ""
    selection_reasoning: str = ""
    evaluations: dict[str, StrategySignal] = field(default_factory=dict)

    @property
    def live_signal(self) -> StrategySignal | None:
        if self.selected_strategy in (None, "", "stay_out"):
            return None
        return self.evaluations.get(self.selected_strategy)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in response")
    return json.loads(text[start : end + 1])


def _validate_signal(strategy: str, sig: dict) -> StrategySignal:
    action = sig.get("action", "")
    confidence = float(sig.get("confidence", 0) or 0)

    if action not in VALID_ACTIONS:
        return StrategySignal(
            ok=False, strategy=strategy, reject_reason=f"invalid action: {action}"
        )

    result = StrategySignal(
        ok=True,
        strategy=strategy,
        action=action,
        confidence=confidence,
        reasoning=sig.get("reasoning", ""),
    )

    if action in ("HOLD", "CLOSE"):
        return result

    try:
        entry = float(sig["entry"])
        stop_loss = float(sig["stop_loss"])
        take_profit = float(sig["take_profit"])
    except (KeyError, TypeError, ValueError):
        return StrategySignal(
            ok=False,
            strategy=strategy,
            reject_reason="missing/invalid entry, stop_loss, or take_profit",
        )

    if action == "BUY" and not (stop_loss < entry < take_profit):
        return StrategySignal(
            ok=False,
            strategy=strategy,
            reject_reason="BUY requires stop_loss < entry < take_profit",
        )
    if action == "SELL" and not (take_profit < entry < stop_loss):
        return StrategySignal(
            ok=False,
            strategy=strategy,
            reject_reason="SELL requires take_profit < entry < stop_loss",
        )

    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    if risk <= 0:
        return StrategySignal(
            ok=False, strategy=strategy, reject_reason="zero-distance stop loss"
        )
    risk_reward = reward / risk

    if risk_reward < settings.min_risk_reward:
        return StrategySignal(
            ok=False,
            strategy=strategy,
            reject_reason=f"risk_reward {risk_reward:.2f} below minimum "
            f"{settings.min_risk_reward}",
        )
    if confidence < settings.min_confidence:
        return StrategySignal(
            ok=False,
            strategy=strategy,
            reject_reason=f"confidence {confidence:.2f} below minimum "
            f"{settings.min_confidence}",
        )

    result.entry = entry
    result.stop_loss = stop_loss
    result.take_profit = take_profit
    result.risk_reward = risk_reward
    return result


def parse_and_validate(raw_text: str) -> ParsedCycle:
    try:
        data = _extract_json(raw_text)
    except Exception as exc:
        return ParsedCycle(ok=False, reject_reason=f"invalid JSON: {exc}")

    regime = data.get("regime", "")
    selected_strategy = data.get("selected_strategy", "")
    raw_evals = data.get("strategy_evaluations", {}) or {}

    if regime not in VALID_REGIMES:
        return ParsedCycle(ok=False, reject_reason=f"invalid regime: {regime}", raw=data)
    if selected_strategy not in ALL_SELECTIONS:
        return ParsedCycle(
            ok=False,
            reject_reason=f"invalid selected_strategy: {selected_strategy}",
            raw=data,
        )

    evaluations = {}
    for strategy in STRATEGIES:
        sig = raw_evals.get(strategy)
        if not isinstance(sig, dict):
            evaluations[strategy] = StrategySignal(
                ok=False, strategy=strategy, reject_reason="missing evaluation"
            )
        else:
            evaluations[strategy] = _validate_signal(strategy, sig)

    return ParsedCycle(
        ok=True,
        raw=data,
        regime=regime,
        regime_reasoning=data.get("regime_reasoning", ""),
        selected_strategy=selected_strategy,
        selection_reasoning=data.get("selection_reasoning", ""),
        evaluations=evaluations,
    )
