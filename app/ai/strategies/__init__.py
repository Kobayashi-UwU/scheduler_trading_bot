from app.ai.strategies.breakout import BREAKOUT
from app.ai.strategies.mean_reversion import MEAN_REVERSION
from app.ai.strategies.sr_bounce import SR_BOUNCE
from app.ai.strategies.trend_following import TREND_FOLLOWING

STRATEGIES = {
    "trend_following": TREND_FOLLOWING,
    "mean_reversion": MEAN_REVERSION,
    "breakout": BREAKOUT,
    "sr_bounce": SR_BOUNCE,
}

STRATEGY_VERSION = "v1"
