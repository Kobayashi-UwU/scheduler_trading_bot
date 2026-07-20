BREAKOUT = """
STRATEGY: breakout (v1)
Fits when: regime is volatile or a tight consolidation is resolving — price closes
beyond a recent swing_high/swing_low with expanding ATR14 (current ATR14 notably
above its recent average) or a visible momentum candle.

RULES (apply exactly, do not invent variations):
1. Only trade in the direction of the break: BUY on a close above swing_high, SELL
   on a close below swing_low.
2. Require confirmation: the breakout candle should close beyond the level, not just
   wick through it.
3. Stop loss: back inside the broken level (just beyond the old swing_high/low),
   roughly 1x ATR14.
4. Take profit: minimum 2R, measured move (range height projected from the break) if
   larger.
5. If price is mid-range with no level nearby to break, action is HOLD.
6. Avoid entering after price has already moved far past the breakout level (chasing).
"""
