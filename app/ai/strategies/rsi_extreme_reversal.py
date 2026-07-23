RSI_EXTREME_REVERSAL = """
STRATEGY: rsi_extreme_reversal (v1)
Fits when: 15m RSI14 is at a statistical extreme (>75 or <25) regardless of whether
price is inside an established range — a sharp, likely-overextended short-term move.

RULES (apply exactly, do not invent variations):
1. SELL when 15m RSI14 > 75 and price is stretched above 15m EMA20. BUY when 15m
   RSI14 < 25 and price is stretched below 15m EMA20.
2. This is a short-term fade, not a range trade: it does not require a defined
   swing_high/swing_low range like mean_reversion does.
3. Stop loss: 1x ATR14(15m) beyond the recent extreme high/low.
4. Take profit: back to 15m EMA20 only — this strategy takes quick, modest targets,
   minimum 1.5R. Do not hold for a larger move.
5. If the 1d trend strongly agrees with the extreme (e.g. RSI high during a powerful
   1d uptrend), action is HOLD — don't fade a strong higher-timeframe trend.
6. If RSI14 on 15m is not at an extreme, action is HOLD.
"""
