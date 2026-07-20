TREND_FOLLOWING = """
STRATEGY: trend_following (v1)
Fits when: regime is trending_up or trending_down, with 1h and 1d EMAs aligned in
the same direction (price > EMA20 > EMA50 for up; reverse for down).

RULES (apply exactly, do not invent variations):
1. Only trade in the direction of the 1h AND 1d trend. If they disagree, do not use
   this strategy.
2. Enter on a pullback: 15m price pulls back toward the 15m EMA20 (not a breakout,
   not the extreme of the move).
3. Stop loss: beyond the most recent 15m swing low (for longs) / swing high (for
   shorts), or 1x ATR14(15m) beyond entry, whichever is farther from entry.
4. Take profit: at minimum 2x the stop distance (2R). May extend to the next
   significant swing level if farther.
5. If price is already extended far from EMA20 (no pullback available), action is
   HOLD — do not chase.
"""
