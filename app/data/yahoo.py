import logging

import pandas as pd
import yfinance as yf

from app.config import settings

log = logging.getLogger("yahoo")

TIMEFRAMES = {
    "15m": {"interval": "15m", "period": "5d"},
    "1h": {"interval": "60m", "period": "1mo"},
    "1d": {"interval": "1d", "period": "6mo"},
}


def _download(symbol: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(
        symbol,
        interval=interval,
        period=period,
        progress=False,
        auto_adjust=True,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    return df[["open", "high", "low", "close", "volume"]].dropna()


def fetch_candles(timeframe: str) -> pd.DataFrame:
    """Fetch candles for a timeframe, falling back to the alt symbol on failure."""
    cfg = TIMEFRAMES[timeframe]
    for symbol in (settings.symbol, settings.fallback_symbol):
        try:
            df = _download(symbol, cfg["interval"], cfg["period"])
            if not df.empty:
                return df
        except Exception as exc:
            log.warning("fetch failed for %s (%s): %s", symbol, timeframe, exc)
    log.error("no candle data for timeframe %s from any symbol", timeframe)
    return pd.DataFrame()


def fetch_all_timeframes() -> dict[str, pd.DataFrame]:
    return {tf: fetch_candles(tf) for tf in TIMEFRAMES}


def get_current_price() -> float | None:
    df = fetch_candles("15m")
    if df.empty:
        return None
    return float(df["close"].iloc[-1])


def is_market_open() -> bool:
    """Gold futures: closed roughly Fri 22:00 UTC -> Sun 23:00 UTC."""
    import datetime as dt

    now = dt.datetime.utcnow()
    weekday = now.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:  # Saturday
        return False
    if weekday == 4 and now.hour >= 22:  # Friday after 22:00
        return False
    if weekday == 6 and now.hour < 23:  # Sunday before 23:00
        return False
    return True
