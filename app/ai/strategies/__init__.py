from app.ai.strategies.atr_squeeze import ATR_SQUEEZE
from app.ai.strategies.breakout import BREAKOUT
from app.ai.strategies.daily_trend_pullback import DAILY_TREND_PULLBACK
from app.ai.strategies.ema_cross import EMA_CROSS
from app.ai.strategies.exhaustion_fade import EXHAUSTION_FADE
from app.ai.strategies.macd_zero_cross import MACD_ZERO_CROSS
from app.ai.strategies.mean_reversion import MEAN_REVERSION
from app.ai.strategies.rsi_extreme_reversal import RSI_EXTREME_REVERSAL
from app.ai.strategies.sr_bounce import SR_BOUNCE
from app.ai.strategies.trend_following import TREND_FOLLOWING

STRATEGIES = {
    "trend_following": TREND_FOLLOWING,
    "mean_reversion": MEAN_REVERSION,
    "breakout": BREAKOUT,
    "sr_bounce": SR_BOUNCE,
    "ema_cross": EMA_CROSS,
    "rsi_extreme_reversal": RSI_EXTREME_REVERSAL,
    "daily_trend_pullback": DAILY_TREND_PULLBACK,
    "atr_squeeze": ATR_SQUEEZE,
    "macd_zero_cross": MACD_ZERO_CROSS,
    "exhaustion_fade": EXHAUSTION_FADE,
}

STRATEGY_VERSION = "v1"
