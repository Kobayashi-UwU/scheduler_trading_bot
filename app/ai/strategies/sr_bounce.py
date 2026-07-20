SR_BOUNCE = """
STRATEGY: sr_bounce (v1)
Fits when: price on the 1d timeframe approaches a well-established support or
resistance level (a level tested 2+ times previously), regardless of broader trend.

RULES (apply exactly, do not invent variations):
1. Identify the nearest major daily support/resistance level from swing_high/
   swing_low and recent price history.
2. BUY on a bounce reaction at support (price approaches, shows rejection: long
   lower wick or reversal candle on 15m/1h). SELL on a bounce at resistance
   (rejection: long upper wick).
3. Stop loss: just beyond the level (0.5x-1x ATR14 buffer).
4. Take profit: minimum 1.5R, targeting the next visible level or range midpoint.
5. If price is approaching the level but has NOT yet shown a rejection reaction,
   action is HOLD — wait for confirmation, don't anticipate.
6. If price breaks cleanly through the level instead of bouncing, do not use this
   strategy.
"""
