EXHAUSTION_FADE = """
STRATEGY: exhaustion_fade (v1)
Fits when: price is extended far from the 15m EMA20 (a large move already played out)
while momentum is decelerating (1h macd_hist shrinking in magnitude even though price
is still extended) — a countertrend fade of an overextended, tiring move. This is the
opposite read of breakout/trend_following, which chase the same kind of move.

RULES (apply exactly, do not invent variations):
1. SELL when price is well above 15m EMA20 (multiple ATR14(15m) away) AND 1h macd_hist
   is positive but shrinking versus its recent bars. BUY on the mirror-image case
   below EMA20 with shrinking negative macd_hist.
2. Stop loss: 1x ATR14(15m) beyond the current extreme (recent swing_high/swing_low).
3. Take profit: back toward 15m EMA20 only, minimum 1.5R — this is a fade back to the
   mean, not a new trend call.
4. If momentum (macd_hist) is still expanding in the direction of the move (not
   decelerating), action is HOLD — the move may still be healthy, don't fade it.
5. If price is not meaningfully extended from EMA20, action is HOLD.
"""
