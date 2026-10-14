from typing import List, Dict, Any
import numpy as np
import pandas as pd


def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """Calculates Relative Strength Index (RSI) for a list of closing prices."""
    if not prices or len(prices) < period + 1:
        return 50.0

    series = pd.Series(prices)
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    valid_g = avg_gain.dropna()
    valid_l = avg_loss.dropna()
    if valid_g.empty or valid_l.empty:
        return 50.0

    last_gain = float(valid_g.iloc[-1])
    last_loss = float(valid_l.iloc[-1])
    if last_gain == 0.0 and last_loss == 0.0:
        return 50.0
    if last_loss == 0.0:
        return 100.0

    rs = last_gain / last_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi) if not np.isnan(rsi) else 50.0


def calculate_sma(prices: List[float], period: int = 50) -> float:
    """Calculates Simple Moving Average (SMA)."""
    if not prices or len(prices) < period:
        return prices[-1] if prices else 0.0
    return float(np.mean(prices[-period:]))


def calculate_ema(prices: List[float], period: int = 20) -> float:
    """Calculates Exponential Moving Average (EMA)."""
    if not prices or len(prices) < period:
        return prices[-1] if prices else 0.0
    series = pd.Series(prices)
    ema = series.ewm(span=period, adjust=False).mean()
    return float(ema.iloc[-1])


def calculate_bollinger_bands(prices: List[float], period: int = 20, num_std: float = 2.0) -> Dict[str, float]:
    """Calculates Bollinger Bands (Middle Band, Upper Band, Lower Band)."""
    if not prices or len(prices) < period:
        latest = prices[-1] if prices else 0.0
        return {"middle": latest, "upper": latest, "lower": latest, "percent_b": 0.5}

    series = pd.Series(prices)
    middle_band = series.rolling(window=period).mean()
    std_dev = series.rolling(window=period).std()

    upper_band = middle_band + (num_std * std_dev)
    lower_band = middle_band - (num_std * std_dev)

    latest_price = prices[-1]
    mb = float(middle_band.iloc[-1])
    ub = float(upper_band.iloc[-1])
    lb = float(lower_band.iloc[-1])

    bandwidth = ub - lb
    percent_b = (latest_price - lb) / bandwidth if bandwidth > 0 else 0.5

    return {
        "middle": round(mb, 2),
        "upper": round(ub, 2),
        "lower": round(lb, 2),
        "percent_b": round(float(percent_b), 2),
    }
