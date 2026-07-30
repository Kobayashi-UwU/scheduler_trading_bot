"""Position sizing for the real-account tier.

The paper tier sizes continuously: `size = risk_amount / stop_distance` in troy
ounces, so any risk target is always hit exactly. A real broker account cannot do
that — volume comes in fixed lot steps with a hard minimum, and every position
consumes margin. On XAUUSD 1.00 lot is 100 oz, so the smallest tradeable position
is 0.01 lot = 1 oz, and *that* position's loss is simply the stop distance in
dollars. A $14 gold stop therefore costs $14 whatever the account is worth, which
on a $100 balance is 14% — the configured `real_risk_per_trade` of 1% is
unreachable and sizing floors at the minimum instead.

`size_real` is where that collision is resolved and made explicit.
"""

import math
from dataclasses import dataclass

from app.config import settings


@dataclass
class Sizing:
    """Outcome of sizing one signal for a real account."""

    ok: bool
    reason: str = ""
    lots: float = 0.0
    size: float = 0.0  # position size in troy oz (lots * contract_size)
    risk_amount: float = 0.0  # dollars lost if the stop is hit
    risk_pct: float = 0.0  # risk_amount as a fraction of balance
    margin: float = 0.0  # dollars of margin the position ties up
    target_risk: float = 0.0  # what real_risk_per_trade asked for
    floored_to_min: bool = False  # True when the risk target was unreachable


def _round_down_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    # +1e-9 absorbs float error so e.g. 0.03/0.01 doesn't land on 2.9999...
    return round(math.floor(value / step + 1e-9) * step, 8)


def size_real(balance: float, entry: float, stop_loss: float) -> Sizing:
    """Size a real-account position, or explain why it cannot be taken."""
    contract = settings.real_contract_size
    min_lot = settings.real_min_lot
    max_lot = settings.real_max_lot

    if balance <= 0:
        return Sizing(ok=False, reason=f"account depleted (balance {balance:.2f})")

    risk_distance = abs(entry - stop_loss)
    if risk_distance <= 0:
        return Sizing(ok=False, reason="zero-distance stop loss")

    # Loss in dollars if a full 1.00 lot were stopped out.
    risk_per_lot = contract * risk_distance
    target_risk = balance * settings.real_risk_per_trade

    lots = _round_down_to_step(target_risk / risk_per_lot, settings.real_lot_step)

    floored_to_min = False
    if lots < min_lot:
        # Broker minimum wins: we cannot express the intended risk, only exceed it.
        lots = min_lot
        floored_to_min = True
    if lots > max_lot:
        lots = max_lot

    risk_amount = lots * risk_per_lot
    risk_pct = risk_amount / balance
    margin = (lots * contract * entry) / settings.real_leverage

    if risk_pct > settings.real_max_risk_pct:
        return Sizing(
            ok=False,
            reason=(
                f"min lot {lots:.2f} risks {risk_amount:.2f} "
                f"({risk_pct * 100:.1f}% of {balance:.2f}), over cap "
                f"{settings.real_max_risk_pct * 100:.0f}%"
            ),
            lots=lots,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            margin=margin,
            target_risk=target_risk,
            floored_to_min=floored_to_min,
        )

    # Only one position per account, so the whole balance is free margin.
    if margin > balance:
        return Sizing(
            ok=False,
            reason=(
                f"margin {margin:.2f} for {lots:.2f} lot exceeds balance "
                f"{balance:.2f} at 1:{settings.real_leverage:.0f} leverage"
            ),
            lots=lots,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            margin=margin,
            target_risk=target_risk,
            floored_to_min=floored_to_min,
        )

    return Sizing(
        ok=True,
        lots=lots,
        size=lots * contract,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        margin=margin,
        target_risk=target_risk,
        floored_to_min=floored_to_min,
    )
