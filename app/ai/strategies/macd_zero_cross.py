MACD_ZERO_CROSS = """
STRATEGY: macd_zero_cross (v1)
Fits when: the 1h MACD histogram is crossing (or has just crossed) zero — an early
momentum-shift signal, traded before EMAs have had time to align (that later stage
belongs to trend_following/ema_cross).

RULES (apply exactly, do not invent variations):
1. BUY when 1h macd_hist has just turned positive after being negative. SELL when it
   has just turned negative after being positive.
2. Require the 15m trend field to not be strongly opposed to the new direction (e.g.
   don't BUY if 15m trend is clearly "bearish").
3. Stop loss: 1x ATR14(1h) beyond entry, or the nearest 1h swing low/high, whichever
   is farther.
4. Take profit: minimum 2R.
5. If macd_hist crossed zero more than a few bars ago (not a fresh cross) or is
   oscillating repeatedly around zero (choppy, no follow-through), action is HOLD.
"""
