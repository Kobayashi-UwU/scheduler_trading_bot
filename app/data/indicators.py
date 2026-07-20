import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast=12, slow=26, signal=9):
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def swing_levels(df: pd.DataFrame, lookback: int = 40) -> dict:
    recent = df.tail(lookback)
    return {
        "swing_high": float(recent["high"].max()),
        "swing_low": float(recent["low"].min()),
    }


def summarize(df: pd.DataFrame) -> dict:
    """Compute a compact indicator summary for the AI prompt."""
    if df.empty or len(df) < 30:
        return {}

    close = df["close"]
    last = float(close.iloc[-1])
    ema20 = ema(close, 20).iloc[-1]
    ema50 = ema(close, 50).iloc[-1] if len(df) >= 50 else None
    ema200 = ema(close, 200).iloc[-1] if len(df) >= 200 else None
    rsi14 = rsi(close, 14).iloc[-1]
    macd_line, signal_line, hist = macd(close)
    atr14 = atr(df, 14).iloc[-1]
    levels = swing_levels(df)

    prev_close = float(close.iloc[-2]) if len(close) > 1 else last
    change_pct = ((last - prev_close) / prev_close) * 100 if prev_close else 0.0

    trend = "unclear"
    if ema50 is not None:
        if last > ema20 > ema50:
            trend = "bullish"
        elif last < ema20 < ema50:
            trend = "bearish"
        else:
            trend = "mixed"

    return {
        "last_close": round(last, 2),
        "change_pct": round(change_pct, 3),
        "ema20": round(float(ema20), 2),
        "ema50": round(float(ema50), 2) if ema50 is not None else None,
        "ema200": round(float(ema200), 2) if ema200 is not None else None,
        "rsi14": round(float(rsi14), 1) if not np.isnan(rsi14) else None,
        "macd_hist": round(float(hist.iloc[-1]), 3) if not np.isnan(hist.iloc[-1]) else None,
        "atr14": round(float(atr14), 2) if not np.isnan(atr14) else None,
        "trend": trend,
        "swing_high": round(levels["swing_high"], 2),
        "swing_low": round(levels["swing_low"], 2),
    }
