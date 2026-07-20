MEAN_REVERSION = """
STRATEGY: mean_reversion (v1)
Fits when: regime is ranging — price oscillating between a clear swing_high and
swing_low on the 1h timeframe with no strong 1d trend.

RULES (apply exactly, do not invent variations):
1. Only trade near the edges of the established range (within ~0.3x ATR14 of
   swing_high or swing_low).
2. SELL near swing_high (RSI14 on 1h ideally > 65). BUY near swing_low (RSI14 on
   1h ideally < 35).
3. Stop loss: just beyond the swing_high/swing_low (0.5x ATR14 buffer).
4. Take profit: the middle of the range, or the opposite edge if risk/reward allows;
   minimum 1.5R.
5. If price is in the middle of the range (not near an edge), action is HOLD.
6. If the range has clearly broken (price closes beyond swing_high/low), do not use
   this strategy — that's a breakout situation instead.
"""
