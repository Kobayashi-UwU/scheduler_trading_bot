import logging

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    nvidia_api_key: str
    nvidia_model: str = "deepseek-ai/deepseek-v4-flash"
    nvidia_fallback_model: str = "deepseek-ai/deepseek-v4-pro"
    nvidia_api_url: str = "https://integrate.api.nvidia.com/v1/chat/completions"
    nvidia_timeout_sec: float = 180.0
    nvidia_max_tokens: int = 3000
    nvidia_retry_backoff_cap_sec: float = 30.0

    symbol: str = "GC=F"
    fallback_symbol: str = "XAUUSD=X"
    analysis_interval_min: int = 15
    price_check_interval_min: int = 1

    start_balance: float = 10000.0
    risk_per_trade: float = 0.01
    min_confidence: float = 0.6
    min_risk_reward: float = 1.5
    max_open_positions: int = 1

    dashboard_password: str = ""

    # --- real-account tier -------------------------------------------------
    # A second set of accounts that simulates a real broker account instead of
    # the idealised paper one: fixed lot granularity, contract size, and margin.
    # Deliberately isolated from the paper tier so the original experiment keeps
    # running untouched.
    real_enabled: bool = True
    real_accounts: str = "ai_selected,trend_following"
    real_start_balance: float = 100.0
    real_leverage: float = 2000.0  # match the broker account's leverage
    real_contract_size: float = 100.0  # troy oz per 1.00 lot (XAUUSD standard)
    real_min_lot: float = 0.01
    real_lot_step: float = 0.01
    real_max_lot: float = 100.0
    real_risk_per_trade: float = 0.01
    # At 0.01 lot (1 oz) a gold stop of $7-25 is 7-25% of a $100 account, so the
    # 1% target above is usually unreachable — size floors at min_lot instead.
    # This is the ceiling on that overshoot: signals needing more are skipped.
    real_max_risk_pct: float = 0.30

    # --- MT5 executor (mirrors the real tier onto live broker accounts) ----
    # Off by default: the sim runs fine without it. When enabled, a scheduler job
    # reconciles each real account's sim position onto its MT5 login via the
    # mt5linux bridge in the Wine/MT5 container.
    executor_enabled: bool = False
    executor_sync_interval_min: int = 1
    executor_symbol: str = "XAUUSD"
    executor_symbol_fallbacks: str = "XAUUSDm,XAUUSDc"
    executor_magic: int = 620717  # tags our orders; leave alone once live
    executor_deviation_points: int = 50
    # Don't open a mirror for a sim position older than this — late fills at a
    # drifted price would no longer resemble the sim trade.
    executor_max_open_age_min: int = 30
    # JSON: {"real_ai_selected": {"login": 123, "password": "...", "server": "..."}}
    executor_accounts: str = ""
    mt5_bridge_host: str = "mt5.railway.internal"
    mt5_bridge_port: int = 8001

    database_url: str = "sqlite:///./data/trading_bot.db"

    @field_validator("database_url")
    @classmethod
    def _normalize_postgres_scheme(cls, v: str) -> str:
        # Railway/Heroku-style URLs use "postgres://"; SQLAlchemy needs "postgresql://"
        if v.startswith("postgres://"):
            return "postgresql://" + v[len("postgres://") :]
        return v


settings = Settings()

STRATEGIES = [
    "trend_following",
    "mean_reversion",
    "breakout",
    "sr_bounce",
    "ema_cross",
    "rsi_extreme_reversal",
    "daily_trend_pullback",
    "atr_squeeze",
    "macd_zero_cross",
    "exhaustion_fade",
]
ALL_SELECTIONS = STRATEGIES + ["stay_out"]

AI_SELECTED_ACCOUNT = "ai_selected"

# Paper tier: the original experiment. ai_selected + one shadow account per strategy.
PAPER_ACCOUNTS = [AI_SELECTED_ACCOUNT] + STRATEGIES

REAL_PREFIX = "real_"


def real_account_for(source: str) -> str:
    """Real-tier account name mirroring a paper account ("trend_following" -> "real_trend_following")."""
    return f"{REAL_PREFIX}{source}"


def real_source_for(account: str) -> str:
    """Inverse of real_account_for."""
    return account[len(REAL_PREFIX) :]


def is_real_account(account: str) -> bool:
    return account.startswith(REAL_PREFIX)


# Which paper accounts the real tier mirrors. Only these get real-money-shaped
# sizing; every other strategy stays paper-only.
_requested_real = [s.strip() for s in settings.real_accounts.split(",") if s.strip()]
REAL_SOURCES = [s for s in _requested_real if s in PAPER_ACCOUNTS]
if _unknown := [s for s in _requested_real if s not in PAPER_ACCOUNTS]:
    # Loud, because a typo in REAL_ACCOUNTS would otherwise silently mean
    # "that account simply never trades".
    logging.getLogger("config").warning(
        "REAL_ACCOUNTS contains unknown account(s) %s — ignored. Valid: %s",
        _unknown,
        PAPER_ACCOUNTS,
    )
REAL_ACCOUNTS = [real_account_for(s) for s in REAL_SOURCES] if settings.real_enabled else []

ALL_ACCOUNTS = PAPER_ACCOUNTS + REAL_ACCOUNTS


def start_balance_for(account: str) -> float:
    return settings.real_start_balance if is_real_account(account) else settings.start_balance
