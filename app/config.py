from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    nvidia_api_key: str
    nvidia_model: str = "deepseek-ai/deepseek-v4-pro"
    nvidia_api_url: str = "https://integrate.api.nvidia.com/v1/chat/completions"

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

    database_url: str = "sqlite:///./data/trading_bot.db"


settings = Settings()

STRATEGIES = ["trend_following", "mean_reversion", "breakout", "sr_bounce"]
ALL_SELECTIONS = STRATEGIES + ["stay_out"]
