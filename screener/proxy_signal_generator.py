"""
Historical Proxy-Signal Generator for Helsinki Nasdaq Small Cap & First North Equities.

Adapts the 'Pump Hunter' volume-surge anomaly strategy from quantitative crypto architectures:
1. Calculates a 20-day rolling median volume baseline.
2. Identifies volume surge anomalies: Daily Volume >= 3.0 * (20-day median volume).
3. Requires positive price momentum: Close > Open and Daily Return >= +2.0%.
4. Applies liquidity floor: 20-day median volume > 0 (excludes dead stocks).
5. Exports chronological signals to data/proxy_signals.csv formatted for backtest_engine.py.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

import numpy as np
import pandas as pd
import yfinance as yf

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("screener.proxy_signal_generator")

# Default Finnish Micro/Small-Cap and First North universe
DEFAULT_HELSINKI_UNIVERSE = [
    "FARON.HE",      # Faron Pharmaceuticals (Biotech First North)
    "RAUTE.HE",      # Raute Oyj (Small Cap Tech/Industrial)
    "INCAP.HE",      # Incap Oyj (EMS / Electronics)
    "FSECURE.HE",    # F-Secure Oyj (Cybersecurity Small Cap)
    "SOLTEQ.HE",     # Solteq Oyj (IT Services Small Cap)
    "RELAIS.HE",     # Relais Group Oyj (Automotive Small Cap)
    "KAMUX.HE",      # Kamux Oyj (Retail Small Cap)
    "NEXSTIM.HE",    # Nexstim Oyj (Medtech First North)
    "BIOHIT.HE",     # Biohit Oyj (Diagnostics Small Cap)
    "DIGIA.HE",      # Digia Oyj (IT Services Small Cap)
    "TECTIA.HE",     # SSH Communications / Tectia (Cybersecurity Small Cap)
    "ROBIT.HE",      # Robit Oyj (Mining Equipment Small Cap)
    "WULFF.HE",      # Wulff-Yhtiöt Oyj (Workplace Services Small Cap)
    "EEZY.HE",       # Eezy Oyj (Staffing Services Small Cap)
    "HARVIA.HE",     # Harvia Oyj (Sauna / Wellness Mid/Small Cap)
    "QTCOM.HE",      # Qt Group Oyj (Software Mid/Small Cap)
    "KEMIRA.HE",     # Kemira Oyj (Chemicals Mid Cap)
    "BITI.HE",       # Bittium Oyj (Defense & Wireless Small Cap)
]

DEFAULT_PROXY_SIGNALS_CSV = Path(__file__).resolve().parent.parent / "data" / "proxy_signals.csv"


@dataclass
class ProxySignal:
    ticker: str
    signal_date: str
    signal_type: str
    volume_surge_ratio: float
    daily_return_pct: float
    daily_volume: int
    rolling_median_vol: int
    close_price: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HistoricalProxySignalGenerator:
    """
    Scans historical daily OHLCV series for volume-surge and momentum anomalies.
    """

    def __init__(
        self,
        tickers: Optional[List[str]] = None,
        surge_multiplier: float = 3.0,
        min_daily_return_pct: float = 2.0,
        rolling_window_days: int = 20,
        min_median_volume: int = 1000,
        lookback_years: float = 2.0,
        output_csv_path: Optional[Path | str] = None,
    ):
        self.tickers = tickers or DEFAULT_HELSINKI_UNIVERSE
        self.surge_multiplier = surge_multiplier
        self.min_daily_return_pct = min_daily_return_pct
        self.rolling_window_days = rolling_window_days
        self.min_median_volume = min_median_volume
        self.lookback_years = lookback_years
        self.output_csv_path = Path(output_csv_path or DEFAULT_PROXY_SIGNALS_CSV)
        self.output_csv_path.parent.mkdir(parents=True, exist_ok=True)

    def generate_proxy_signals(self) -> List[ProxySignal]:
        """
        Fetches historical data, computes rolling metrics, detects anomalies,
        and saves signals to CSV.
        """
        logger.info("=" * 80)
        logger.info(">>> RUNNING HISTORICAL PROXY-SIGNAL GENERATOR (PUMP HUNTER) <<<")
        logger.info(f"Target Universe: {len(self.tickers)} Helsinki tickers | Lookback: {self.lookback_years:.1f} years")
        logger.info(f"Anomaly Rules: Volume >= {self.surge_multiplier:.1f}x (20d Median) & Return >= +{self.min_daily_return_pct:.1f}%")
        logger.info("=" * 80)

        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=int(self.lookback_years * 365.25) + 30)

        all_signals: List[ProxySignal] = []
        ticker_counts: Dict[str, int] = {}

        for idx, ticker in enumerate(self.tickers, start=1):
            symbol = ticker.upper().strip()
            if not symbol.endswith(".HE") and "." not in symbol:
                symbol = f"{symbol}.HE"

            logger.info(f"[{idx}/{len(self.tickers)}] Downloading and scanning {symbol}...")
            try:
                df = yf.download(
                    symbol,
                    start=start_date.isoformat(),
                    end=end_date.isoformat(),
                    progress=False,
                )
            except Exception as e:
                logger.warning(f"Error fetching data for {symbol}: {e}")
                continue

            if df is None or df.empty or len(df) < self.rolling_window_days + 5:
                logger.warning(f"Insufficient historical data for {symbol} (got {len(df) if df is not None else 0} bars).")
                continue

            # Flatten multi-index columns if returned by yfinance
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]

            # Standardize lowercase column names
            df.columns = [str(c).lower() for c in df.columns]

            # Drop missing values in critical columns
            df = df.dropna(subset=["open", "high", "low", "close", "volume"])

            ticker_signals = self.detect_signals_in_dataframe(symbol, df)
            all_signals.extend(ticker_signals)
            ticker_counts[symbol] = len(ticker_signals)
            logger.info(f"Found {len(ticker_signals)} proxy volume anomaly signals in {symbol}.")

        # Sort all signals chronologically
        all_signals.sort(key=lambda s: (s.signal_date, s.ticker))

        # Save to CSV
        self.save_signals_to_csv(all_signals)

        # Print summary
        self.print_summary(all_signals, ticker_counts)

        return all_signals

    def detect_signals_in_dataframe(self, ticker: str, df: pd.DataFrame) -> List[ProxySignal]:
        """
        Applies mathematical anomaly detection on a single ticker OHLCV DataFrame.
        """
        signals: List[ProxySignal] = []
        if len(df) < self.rolling_window_days + 1:
            return signals

        # Calculate 20-day rolling median volume (excluding current bar using shift)
        rolling_median_vol = df["volume"].shift(1).rolling(window=self.rolling_window_days).median()

        # Calculate intra-day momentum (Open to Close return) and Inter-day return (Close to Prev Close)
        intraday_return_pct = ((df["close"] - df["open"]) / df["open"]) * 100.0
        interday_return_pct = df["close"].pct_change() * 100.0

        for i in range(self.rolling_window_days + 1, len(df)):
            cur_date = df.index[i]
            date_str = pd.to_datetime(cur_date).strftime("%Y-%m-%d")

            cur_volume = float(df["volume"].iloc[i])
            med_vol = float(rolling_median_vol.iloc[i])
            cur_open = float(df["open"].iloc[i])
            cur_close = float(df["close"].iloc[i])
            cur_return = float(intraday_return_pct.iloc[i])
            cur_inter_return = float(interday_return_pct.iloc[i])

            # Condition 3: Liquidity Floor (median volume > 0 and >= threshold)
            if np.isnan(med_vol) or med_vol < self.min_median_volume:
                continue

            # Condition 1: Volume Surge Ratio >= 3.0x
            surge_ratio = cur_volume / med_vol
            if surge_ratio < self.surge_multiplier:
                continue

            # Condition 2: Positive Momentum (Close > Open and Return >= min_daily_return_pct)
            is_positive_candle = cur_close > cur_open
            has_momentum = cur_return >= self.min_daily_return_pct or cur_inter_return >= self.min_daily_return_pct

            if is_positive_candle and has_momentum:
                # Classify proxy signal type based on magnitude
                sig_type = "POSITIVE_GUIDANCE" if cur_return >= 5.0 else "INSIDER_BUYING"
                
                sig = ProxySignal(
                    ticker=ticker,
                    signal_date=date_str,
                    signal_type=sig_type,
                    volume_surge_ratio=round(float(surge_ratio), 2),
                    daily_return_pct=round(float(cur_return), 2),
                    daily_volume=int(cur_volume),
                    rolling_median_vol=int(med_vol),
                    close_price=round(float(cur_close), 4),
                )
                signals.append(sig)

        return signals

    def save_signals_to_csv(self, signals: List[ProxySignal]) -> None:
        """Saves generated proxy signals to CSV."""
        fieldnames = [
            "ticker",
            "signal_date",
            "signal_type",
            "volume_surge_ratio",
            "daily_return_pct",
            "daily_volume",
            "rolling_median_vol",
            "close_price",
        ]
        with open(self.output_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for s in signals:
                writer.writerow(s.to_dict())

        logger.info(f"Successfully saved {len(signals)} proxy signals to {self.output_csv_path}")

    def print_summary(self, signals: List[ProxySignal], ticker_counts: Dict[str, int]) -> None:
        """Prints formatted console summary of discovered proxy signals."""
        print("\n" + "=" * 80)
        print("          HISTORICAL PROXY SIGNALS DISCOVERY SUMMARY (PUMP HUNTER)          ")
        print("=" * 80)
        print(f"Total Tickers Scanned:           {len(self.tickers)}")
        print(f"Total Proxy Signals Discovered:  {len(signals)}")
        print("-" * 80)
        print(f"{'Ticker Symbol':<18} | {'Discovered Signals':<20} | {'Status'}")
        print("-" * 80)

        for ticker in sorted(self.tickers):
            symbol = ticker if "." in ticker else f"{ticker}.HE"
            count = ticker_counts.get(symbol, 0)
            status = f"{count} anomalies" if count > 0 else "No anomalies"
            print(f"{symbol:<18} | {count:<20} | {status}")

        print("-" * 80)
        if signals:
            avg_surge = float(np.mean([s.volume_surge_ratio for s in signals]))
            avg_return = float(np.mean([s.daily_return_pct for s in signals]))
            print(f"Average Volume Surge Ratio:      {avg_surge:.2f}x (20d Median)")
            print(f"Average Anomaly Day Return:      +{avg_return:.2f}%")
            print(f"Exported Dataset Path:           {self.output_csv_path}")
        print("=" * 80 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Historical Proxy-Signal Generator for Helsinki Micro-Caps")
    parser.add_argument("--output", type=str, default="data/proxy_signals.csv", help="Path to output signals CSV")
    parser.add_argument("--surge", type=float, default=3.0, help="Volume surge multiplier threshold (default 3.0x)")
    parser.add_argument("--min-return", type=float, default=2.0, help="Minimum positive return pct (default 2.0%)")
    parser.add_argument("--lookback", type=float, default=2.0, help="Lookback period in years (default 2.0)")

    args = parser.parse_args()

    generator = HistoricalProxySignalGenerator(
        surge_multiplier=args.surge,
        min_daily_return_pct=args.min_return,
        lookback_years=args.lookback,
        output_csv_path=args.output,
    )
    generator.generate_proxy_signals()


if __name__ == "__main__":
    main()
