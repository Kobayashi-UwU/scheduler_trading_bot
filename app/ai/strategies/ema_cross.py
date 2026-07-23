EMA_CROSS = """
STRATEGY: ema_cross (v1)
Fits when: the 1h EMA20 and EMA50 are converging or have just crossed (a fresh
directional shift), rather than already fully aligned and extended like a mature trend.

RULES (apply exactly, do not invent variations):
1. BUY when 1h EMA20 has crossed above EMA50 (or is within ~0.2x ATR14 of crossing)
   and macd_hist on 1h is positive. SELL on the mirror-image bearish cross.
2. Enter on the 15m candle close in the direction of the cross; do not anticipate
   the cross before it happens.
3. Stop loss: just beyond the most recent 1h swing low (for longs) / swing high (for
   shorts), or 1x ATR14(1h), whichever is farther from entry.
4. Take profit: minimum 2R, or the next daily swing level if farther.
5. If the EMAs are already far apart (mature, extended trend) rather than freshly
   crossing, action is HOLD — that setup belongs to trend_following, not this strategy.
"""
