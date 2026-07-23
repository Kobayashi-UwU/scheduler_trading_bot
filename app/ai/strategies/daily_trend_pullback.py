DAILY_TREND_PULLBACK = """
STRATEGY: daily_trend_pullback (v1)
Fits when: the 1d timeframe shows a strong, mature trend (EMA20 > EMA50 > EMA200 for
up, reverse for down, all clearly separated) and price pulls back on the 1h timeframe
— this is a higher-timeframe swing trade, not the faster 15m pullback used by
trend_following.

RULES (apply exactly, do not invent variations):
1. Only trade in the direction of the 1d trend.
2. Enter on a pullback to the 1h EMA50 (a deeper retracement than trend_following's
   15m EMA20 pullback) with 1h RSI14 not yet oversold/overbought against the trend.
3. Stop loss: beyond the most recent 1h swing low (for longs) / swing high (for
   shorts), or 1.5x ATR14(1h) beyond entry, whichever is farther.
4. Take profit: minimum 2.5R — this strategy holds for larger, swing-style moves than
   trend_following's tighter 15m version.
5. If the 1d EMAs are not clearly separated (flat/mixed), or price hasn't reached the
   1h EMA50, action is HOLD.
"""
