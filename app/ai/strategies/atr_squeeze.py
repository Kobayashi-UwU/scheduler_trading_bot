ATR_SQUEEZE = """
STRATEGY: atr_squeeze (v1)
Fits when: 1h ATR14 is compressed (notably below its recent norm — a quiet,
low-volatility coil), before any confirmed break of a swing level. This strategy
enters the earliest sign of directional expansion, ahead of breakout's stricter
close-beyond-the-level confirmation.

RULES (apply exactly, do not invent variations):
1. Only consider entries when 1h ATR14 is compressed relative to its own recent
   behavior (a tight, quiet range with no strong 1d trend forcing a direction).
2. BUY on the first 15m candle with clearly expanding range and positive change_pct
   in the direction away from the recent squeeze. SELL on the mirror-image case.
3. Stop loss: the opposite side of the squeeze range (1h swing_high/swing_low),
   or 1x ATR14(15m), whichever is closer to entry (this is an early, tighter-risk
   entry than breakout).
4. Take profit: minimum 2R; do not extrapolate a large measured move since the
   expansion is not yet confirmed by a full level break.
5. If ATR14 is not compressed (normal or already expanding volatility), action is
   HOLD — that regime belongs to breakout instead.
"""
