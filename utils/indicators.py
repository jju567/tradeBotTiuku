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

    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))

    val = rsi.iloc[-1]
    return float(val) if not np.isnan(val) else 50.0


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
