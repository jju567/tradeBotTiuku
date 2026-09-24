"""
Streamlit Web Dashboard for tradeBotTiuku Stock Screener & Trading Bot.
Visualizes:
1. Live Screener Results (Dual-Lens LLM evaluations)
2. Turnaround Watchlist (WATCH_TURNAROUND divergence detections)
3. Paper Trading Portfolio & Alerts (Open Positions, Sizing, Trade History)
4. Interactive Live Ticker Scanner (Real-time dual-step inspection)
"""

import os
import sys
import csv
import json
import time
import logging
import zoneinfo
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import quant_analytics as qa

logger = logging.getLogger(__name__)

# Setup Base Paths
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DATA_DIR = BASE_DIR / "data"
BATCH_RESULTS_CSV = DATA_DIR / "batch_results.csv"
WATCHLIST_CSV = DATA_DIR / "watchlist_turnarounds.csv"
OPEN_POSITIONS_CSV = DATA_DIR / "open_positions.csv"
SCREENER_ALERTS_CSV = DATA_DIR / "screener_alerts.csv"
TRADE_HISTORY_CSV = DATA_DIR / "trade_history.csv"
BATCH_STATUS_JSON = DATA_DIR / "batch_status.json"
PAPER_ACCOUNT_JSON = DATA_DIR / "paper_account.json"
PORTFOLIO_HISTORY_JSON = DATA_DIR / "portfolio_history.json"
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
PORTFOLIOS_CONFIG_YAML = BASE_DIR / "portfolios_config.yaml"
OPERATIONAL_METRICS_JSON = DATA_DIR / "operational_metrics.json"

# Page Configuration
st.set_page_config(
    page_title="tradeBotTiuku — Quantitative Screener & Turnaround Hub",
    page_icon="🐱",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Timezone Helper (Helsinki Time with 1-second accuracy)
try:
    HELSINKI_TZ = zoneinfo.ZoneInfo("Europe/Helsinki")
except Exception:
    HELSINKI_TZ = timezone.utc


def to_helsinki_time(val: object) -> str:
    """Converts UTC / ISO timestamp to Finnish local time (DD.MM.YYYY HH:MM:SS)."""
    if pd.isna(val) or not val:
        return ""
    val_str = str(val).strip()
    try:
        if val_str.endswith("Z"):
            val_str = val_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(val_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        
        dt_local = dt.astimezone(HELSINKI_TZ)
        return dt_local.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return val_str



def is_pid_alive(pid: int) -> bool:
    """Checks if a process ID is currently running on the system."""
    if not pid or pid <= 0:
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def get_batch_status() -> dict:
    """Reads live batch execution status from JSON file and validates process liveness."""
    if not BATCH_STATUS_JSON.exists() or BATCH_STATUS_JSON.stat().st_size == 0:
        return {"is_running": False}
    try:
        with open(BATCH_STATUS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("is_running"):
            pid = data.get("pid")
            if pid and not is_pid_alive(pid):
                data["is_running"] = False
        return data
    except Exception:
        return {"is_running": False}


def is_ticker_in_watchlist(watchlist_path: Path, ticker: str) -> bool:
    """Checks if a ticker is already present in watchlist_turnarounds.csv."""
    if not watchlist_path.exists():
        return False
    try:
        with open(watchlist_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if str(row.get("ticker", "")).strip().upper() == str(ticker).strip().upper():
                    return True
    except Exception:
        pass
    return False


def append_to_turnaround_watchlist(watchlist_path: Path, entry: Dict[str, Any]) -> bool:
    """Appends a new turnaround candidate entry to watchlist_turnarounds.csv."""
    try:
        watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = watchlist_path.exists()
        fieldnames = [
            "timestamp", "company_name", "ticker", "strategy_type", "title",
            "link", "doc_verdict", "doc_reasoning", "positive_catalysts_found", "web_reasoning"
        ]
        catalysts = entry.get("positive_catalysts_found", [])
        if isinstance(catalysts, list):
            catalysts_str = "; ".join(catalysts) if catalysts else "Turnaround catalysts detected"
        else:
            catalysts_str = str(catalysts)

        with open(watchlist_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists or watchlist_path.stat().st_size == 0:
                writer.writerow(fieldnames)
            writer.writerow([
                entry.get("timestamp", datetime.now(timezone.utc).isoformat()),
                entry.get("company_name", ""),
                entry.get("ticker", ""),
                entry.get("strategy_type", "TURNAROUND"),
                entry.get("title", ""),
                entry.get("link", ""),
                entry.get("doc_verdict", "MANUAL_SCAN"),
                entry.get("doc_reasoning", ""),
                catalysts_str,
                entry.get("web_reasoning", ""),
            ])
        return True
    except Exception as e:
        logger.error(f"Failed to append to turnaround watchlist {watchlist_path}: {e}")
        return False


def remove_from_turnaround_watchlist(watchlist_path: Path, ticker: str) -> bool:
    """Removes a ticker entry from watchlist_turnarounds.csv."""
    if not watchlist_path.exists():
        return False
    try:
        df = pd.read_csv(watchlist_path)
        if "ticker" in df.columns:
            df = df[df["ticker"].astype(str).str.strip().str.upper() != ticker.strip().upper()]
            df.to_csv(watchlist_path, index=False, encoding="utf-8")
            return True
    except Exception as e:
        logger.error(f"Failed to remove {ticker} from {watchlist_path}: {e}")
    return False

# Custom CSS for modern styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 800;
        background: linear-gradient(90deg, #1e3a8a, #3b82f6);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1.05rem;
        color: #64748b;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 16px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .badge-buy {
        background-color: #dcfce7;
        color: #166534;
        padding: 3px 8px;
        border-radius: 6px;
        font-weight: 700;
    }
    .badge-watch {
        background-color: #ffedd5;
        color: #9a3412;
        padding: 3px 8px;
        border-radius: 6px;
        font-weight: 700;
    }
    .badge-hold {
        background-color: #fef9c3;
        color: #854d0e;
        padding: 3px 8px;
        border-radius: 6px;
        font-weight: 700;
    }
    .badge-reject {
        background-color: #fee2e2;
        color: #991b1b;
        padding: 3px 8px;
        border-radius: 6px;
        font-weight: 700;
    }
    /* Estä Streamlitin harmaantuminen ja latauspeite päivitysten aikana (Anti-Dimming Overlay) */
    .stApp[data-test-script-state="running"] [data-testid="stMain"],
    .stApp[data-test-script-state="running"] [data-testid="stSidebar"],
    .stApp[data-test-script-state="running"] div[data-testid="stAppViewBlockContainer"],
    .stApp[data-test-script-state="running"] div[data-testid="stVerticalBlock"] {
        opacity: 1 !important;
        filter: none !important;
        transition: none !important;
    }
    div[data-testid="stAppViewBlockContainer"] {
        opacity: 1 !important;
        filter: none !important;
    }
    div[data-testid="stStatusWidget"] {
        visibility: hidden !important;
    }
</style>
""", unsafe_allow_html=True)


def load_csv_safely(path: Path, expected_cols: list = None) -> pd.DataFrame:
    """Safely loads a CSV file into DataFrame with lowercase column normalization and fallback."""
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=expected_cols or [])
    try:
        df = pd.read_csv(path, encoding="utf-8")
        df.columns = [str(c).strip().lower() for c in df.columns]
        return df
    except Exception as e:
        st.warning(f"Error loading {path.name}: {e}")
        return pd.DataFrame(columns=expected_cols or [])

KNOWN_ENTRY_PRICES = {
    "VIAFIN.HE": 19.80,
    "SEDANA.ST": 10.54,
    "MOB.ST": 10.60,
    "MSAB-B.ST": 93.00,
    "WATT": 11.73,
    "OSS": 9.18,
    "SSH1V.HE": 2.205,
    "RAUTE.HE": 15.20,
    "STIL.ST": 241.50,
    "SEZI.ST": 2.87,
    "VUZI": 2.76,
    "DUOT": 8.54,
    "HOLO": 1.66,
    "CAMP": 4.24,
    "EGAN": 5.36,
    "GROW": 3.06,
}


@st.cache_data(ttl=60)
def fetch_positions_live_data(tickers_tuple: tuple) -> dict:
    """Fetches real-time price, day change, and currency for a tuple of tickers with fallback logic."""
    from screener.price_fetcher import get_realtime_data
    live_map = {
        "_fetch_timestamp": datetime.now(HELSINKI_TZ).strftime("%d.%m.%Y %H:%M:%S")
    }
    for ticker in tickers_tuple:

        t_clean = str(ticker).strip().upper()
        if not t_clean:
            continue
        try:
            res = get_realtime_data(t_clean)
            if res.get("status") != "SUCCESS" and "." not in t_clean:
                for alt_suffix in [".HE", ".ST"]:
                    alt_res = get_realtime_data(f"{t_clean}{alt_suffix}")
                    if alt_res.get("status") == "SUCCESS":
                        res = alt_res
                        break
            live_map[t_clean] = res
        except Exception as err:
            live_map[t_clean] = {"status": "ERROR", "error": str(err)}
    return live_map


@st.cache_data(ttl=300)
def get_live_fx_rates() -> dict:
    """Fetches live FX rates EURUSD and EURSEK from Yahoo Finance."""
    import yfinance as yf
    fx_dict = {"EURUSD": 1.15, "EURSEK": 11.30}
    try:
        t_usd = yf.Ticker("EURUSD=X").history(period="5d")
        if not t_usd.empty:
            fx_dict["EURUSD"] = float(t_usd["Close"].dropna().iloc[-1])
    except Exception:
        pass
    try:
        t_sek = yf.Ticker("EURSEK=X").history(period="5d")
        if not t_sek.empty:
            fx_dict["EURSEK"] = float(t_sek["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return fx_dict


@st.cache_data(ttl=180)
def fetch_ticker_history(ticker: str, period: str = "3mo") -> tuple:
    """Fetches historical OHLCV data for charts with suffix fallback."""
    import yfinance as yf
    t_clean = str(ticker).strip().upper()
    try:
        t_obj = yf.Ticker(t_clean)
        df_hist = t_obj.history(period=period)
        if df_hist.empty and "." not in t_clean:
            for s in [".HE", ".ST"]:
                t_alt = yf.Ticker(f"{t_clean}{s}")
                df_alt = t_alt.history(period=period)
                if not df_alt.empty:
                    return df_alt, f"{t_clean}{s}"
        return df_hist, t_clean
    except Exception:
        return pd.DataFrame(), t_clean


def record_portfolio_snapshot(
    total_equity: float,
    cash_balance: float,
    stock_value: float,
    starting_balance: float = 10_000.0,
    history_file: Path = PORTFOLIO_HISTORY_JSON,
    has_positions: Optional[bool] = None,
) -> None:
    """Records a new equity snapshot if sufficient time or value delta has occurred."""
    # Sanity filter: Ignore zero/anomalously low readings (e.g. temporary API fetch failure dropping stock value to 0)
    min_reasonable_equity = starting_balance * 0.40  # e.g. 4000 € minimum
    if total_equity <= 0 or total_equity < min_reasonable_equity:
        return

    # Check if open positions exist: if positions exist, stock value cannot be 0.0 or flat 10,000 reset
    try:
        check_pos = has_positions
        if check_pos is None:
            if OPEN_POSITIONS_CSV.exists() and OPEN_POSITIONS_CSV.stat().st_size > 0:
                df_check = pd.read_csv(OPEN_POSITIONS_CSV)
                check_pos = not df_check.empty and len(df_check) > 0
            else:
                check_pos = False
        if check_pos:
            if stock_value <= 0.0 or (abs(total_equity - starting_balance) < 0.01 and stock_value < 100.0):
                # Reject corrupt zero-stock snapshot when positions are actually active
                return
    except Exception:
        pass

    history = []
    if history_file.exists():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    should_append = True
    if history:
        last = history[-1]
        last_dt_str = last.get("timestamp")
        try:
            last_dt = datetime.fromisoformat(str(last_dt_str))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            seconds_diff = abs((now_dt - last_dt).total_seconds())
            val_diff = abs(float(last.get("total_equity", 0.0)) - float(total_equity))
            # Don't flood: only append if >= 30s have elapsed or equity moved >= 0.50 EUR
            if seconds_diff < 30 and val_diff < 0.50:
                should_append = False
        except Exception:
            pass

    if should_append:
        tot_ret = round(total_equity - starting_balance, 2)
        tot_ret_pct = round((tot_ret / starting_balance * 100.0), 2) if starting_balance > 0 else 0.0
        snapshot = {
            "timestamp": now_iso,
            "total_equity": round(total_equity, 2),
            "cash_balance": round(cash_balance, 2),
            "total_stock_value": round(stock_value, 2),
            "total_return": tot_ret,
            "total_return_pct": tot_ret_pct,
        }
        history.append(snapshot)
        if len(history) > 10000:
            history = history[-10000:]
        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


@st.cache_data(ttl=300)
def compute_live_portfolio_history(
    positions_tuple: tuple,
    free_cash: float,
    starting_capital: float = 10_000.0,
    timeframe: str = "Viimeiset 7 päivää",
) -> pd.DataFrame:
    """
    Reconstructs the true continuous portfolio equity curve directly from
    historical market closing prices of the open positions.
    Eliminates corruption from static snapshots and accurately reflects actual stock performance.
    """
    import yfinance as yf

    if not positions_tuple:
        return pd.DataFrame()

    tickers = [p[0] for p in positions_tuple]
    
    # Determine appropriate download period & interval
    if timeframe in ["Viimeiset 24 tuntia", "Viimeiset 7 päivää"]:
        period = "7d"
        interval = "1h"
    elif timeframe == "Viimeiset 30 päivää":
        period = "1mo"
        interval = "1d"
    else:  # "Viimeiset 3 kuukautta", "Kaikki historia"
        period = "3mo"
        interval = "1d"

    try:
        df_raw = yf.download(tickers, period=period, interval=interval, auto_adjust=True, progress=False)
        if df_raw.empty:
            return pd.DataFrame()
        if "Close" in df_raw.columns:
            close_df = df_raw["Close"].copy()
        else:
            close_df = df_raw.copy()

        if isinstance(close_df, pd.Series):
            close_df = close_df.to_frame(name=tickers[0])

        # Standardize timezone to Europe/Helsinki
        if close_df.index.tz is None:
            close_df.index = close_df.index.tz_localize("UTC").tz_convert(HELSINKI_TZ)
        else:
            close_df.index = close_df.index.tz_convert(HELSINKI_TZ)

        close_df = close_df.sort_index()

        # Get exchange rates for conversion to EUR
        fx_rates = get_live_fx_rates()
        eur_usd = fx_rates.get("EURUSD", 1.15)
        eur_sek = fx_rates.get("EURSEK", 11.30)

        # Forward fill and backfill prices per ticker using buy_price fallback
        for t_sym, shares, buy_price, curr_val, curr_code in positions_tuple:
            bp = float(buy_price) if buy_price > 0 else 1.0
            if t_sym in close_df.columns:
                close_df[t_sym] = close_df[t_sym].ffill().bfill().fillna(bp)
            else:
                close_df[t_sym] = bp

        # Compute stock portfolio value series across time
        stock_val_series = pd.Series(0.0, index=close_df.index)
        for t_sym, shares, buy_price, curr_val, curr_code in positions_tuple:
            fx = 1.0
            if curr_code == "USD":
                fx = 1.0 / eur_usd
            elif curr_code == "SEK":
                fx = 1.0 / eur_sek

            stock_val_series += close_df[t_sym] * float(shares) * fx

        res = pd.DataFrame(index=close_df.index)
        res["cash_balance"] = float(free_cash)
        res["total_stock_value"] = stock_val_series
        res["total_equity"] = stock_val_series + float(free_cash)
        res["total_return"] = res["total_equity"] - starting_capital
        res["total_return_pct"] = (res["total_return"] / starting_capital) * 100.0 if starting_capital > 0 else 0.0

        return res
    except Exception:
        return pd.DataFrame()


def render_portfolio_equity_chart(
    df_pos: pd.DataFrame = None,
    free_cash: float = 0.0,
    starting_capital: float = 10_000.0,
    history_file: Path = PORTFOLIO_HISTORY_JSON,
):
    """
    Renders interactive Plotly equity performance chart with multi-interval and metric controls.
    Prioritizes real-time market reconstruction from active positions, falling back to JSON snapshots.
    """
    helsinki_tz = HELSINKI_TZ
    chart_updated_at = datetime.now(helsinki_tz).strftime("%d.%m.%Y %H:%M:%S")

    st.markdown("### 📈 Salkun Kokonaistuloksen ja Varallisuuden Kehitys")
    st.caption("Reaaliaikainen seuranta salkun kokonaisarvon, tuoton ja varallisuuserien kehityksestä suoraan markkinahinnoista.")

    # Controls row
    c_time, c_interval, c_metric = st.columns([1.2, 1.2, 1.6])
    with c_time:
        timeframe_sel = st.selectbox(
            "Aikaväli (Haarukka):",
            [
                "Viimeiset 7 päivää",
                "Viimeiset 24 tuntia",
                "Viimeiset 30 päivää",
                "Viimeiset 3 kuukautta",
                "Kaikki historia",
            ],
            index=0,
            key="equity_timeframe_select",
        )
    with c_interval:
        interval_sel = st.selectbox(
            "Resoluutio / Aggregointi:",
            [
                "Tunti (1h)",
                "Kaikki mittauspisteet",
                "Päivä (1d)",
                "Viikko (1vk)",
                "Kuukausi (1kk)",
            ],
            index=0,
            key="equity_interval_select",
        )
    with c_metric:
        metric_sel = st.selectbox(
            "Näkymä / Mittari:",
            [
                "Salkun Kokonaisarvo (€)",
                "Kokonaistuotto (€ ja %)",
                "Varallisuuden jakautuma (Käteinen vs. Osakkeet)",
            ],
            index=0,
            key="equity_metric_select",
        )

    # 1. Attempt dynamic market reconstruction if positions are active
    df_plot = pd.DataFrame()
    is_live_reconstructed = False

    if df_pos is not None and not df_pos.empty:
        pos_tuples = []
        for _, r in df_pos.iterrows():
            sym = str(r.get("ticker", "")).strip().upper()
            if not sym:
                continue
            sh = float(pd.to_numeric(r.get("shares", 0.0), errors="coerce") or 0.0)
            bp = float(pd.to_numeric(r.get("buy_price", r.get("entry_price", 0.0)), errors="coerce") or 0.0)
            cp = float(pd.to_numeric(r.get("current_price", 0.0), errors="coerce") or bp)
            curr = str(r.get("currency", "USD")).strip().upper()
            pos_tuples.append((sym, sh, bp, cp, curr))

        if pos_tuples:
            df_reconstructed = compute_live_portfolio_history(
                tuple(pos_tuples),
                free_cash=free_cash,
                starting_capital=starting_capital,
                timeframe=timeframe_sel,
            )
            if not df_reconstructed.empty:
                df_plot = df_reconstructed.copy()
                is_live_reconstructed = True

    # 2. Fallback to portfolio_history.json if live reconstruction produced no data
    if df_plot.empty:
        if not history_file.exists():
            st.info("📊 Salkun historiaa kerätään... Ensimmäinen mittauspiste tallennettu.")
            return

        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []

        if not data:
            st.info("📊 Salkun historiatietoja ei vielä saatavilla.")
            return

        df_hist = pd.DataFrame(data)
        if "timestamp" not in df_hist.columns or "total_equity" not in df_hist.columns:
            st.info("📊 Salkun historiadata alustetaan...")
            return

        df_hist["dt"] = pd.to_datetime(df_hist["timestamp"], format="ISO8601", utc=True, errors="coerce")
        df_hist = df_hist.dropna(subset=["dt"])
        if df_hist.empty:
            return

        df_hist["dt_local"] = df_hist["dt"].dt.tz_convert(helsinki_tz)
        df_hist = df_hist.sort_values("dt_local").set_index("dt_local")

        for col in ["cash_balance", "total_stock_value"]:
            if col not in df_hist.columns:
                df_hist[col] = 0.0

        df_hist["total_equity"] = pd.to_numeric(df_hist["total_equity"], errors="coerce").fillna(starting_capital)
        df_hist["cash_balance"] = pd.to_numeric(df_hist["cash_balance"], errors="coerce").fillna(0.0)
        df_hist["total_stock_value"] = pd.to_numeric(df_hist["total_stock_value"], errors="coerce").fillna(0.0)

        # Filter out corrupted / zero-drop glitch data points (e.g. total_equity < 40% of starting balance)
        df_hist = df_hist[df_hist["total_equity"] >= (starting_capital * 0.40)]

        # Drop mid-history points where stock_value dropped to 0 and equity reset to flat starting_capital
        if (df_hist["total_stock_value"] > 0).any():
            first_idx = df_hist.index[0]
            is_glitch_reset = (df_hist.index != first_idx) & (df_hist["total_stock_value"] <= 0.0) & (abs(df_hist["total_equity"] - starting_capital) < 0.01)
            df_hist = df_hist[~is_glitch_reset]

        if df_hist.empty:
            st.info("📊 Salkun historiatietoja ei vielä saatavilla.")
            return

        df_hist["total_return"] = df_hist["total_equity"] - starting_capital
        df_hist["total_return_pct"] = (df_hist["total_return"] / starting_capital * 100.0) if starting_capital > 0 else 0.0

        # Filter Timeframe for JSON history
        now_local = datetime.now(helsinki_tz)
        if timeframe_sel == "Viimeiset 24 tuntia":
            cutoff = now_local - pd.Timedelta(hours=24)
            df_plot = df_hist[df_hist.index >= cutoff]
        elif timeframe_sel == "Viimeiset 7 päivää":
            cutoff = now_local - pd.Timedelta(days=7)
            df_plot = df_hist[df_hist.index >= cutoff]
        elif timeframe_sel == "Viimeiset 30 päivää":
            cutoff = now_local - pd.Timedelta(days=30)
            df_plot = df_hist[df_hist.index >= cutoff]
        elif timeframe_sel == "Viimeiset 3 kuukautta":
            cutoff = now_local - pd.Timedelta(days=90)
            df_plot = df_hist[df_hist.index >= cutoff]
        else:
            df_plot = df_hist

        if df_plot.empty:
            df_plot = df_hist.iloc[-1:]

    # Apply Aggregation / Resampling if requested and possible
    cols_to_resample = ["total_equity", "cash_balance", "total_stock_value", "total_return", "total_return_pct"]
    for c in cols_to_resample:
        if c not in df_plot.columns:
            df_plot[c] = 0.0

    try:
        if interval_sel == "Tunti (1h)":
            df_resampled = df_plot[cols_to_resample].resample("1h").last().ffill().dropna()
            if not df_resampled.empty:
                df_plot = df_resampled
        elif interval_sel == "Päivä (1d)":
            df_resampled = df_plot[cols_to_resample].resample("1D").last().ffill().dropna()
            if not df_resampled.empty:
                df_plot = df_resampled
        elif interval_sel == "Viikko (1vk)":
            df_resampled = df_plot[cols_to_resample].resample("1W").last().ffill().dropna()
            if not df_resampled.empty:
                df_plot = df_resampled
        elif interval_sel == "Kuukausi (1kk)":
            df_resampled = df_plot[cols_to_resample].resample("1ME").last().ffill().dropna()
            if not df_resampled.empty:
                df_plot = df_resampled
    except Exception:
        pass

    latest_data_dt = df_plot.index.max()
    latest_data_at = latest_data_dt.strftime("%d.%m.%Y %H:%M:%S") if pd.notna(latest_data_dt) else "N/A"
    engine_badge = "🟢 Markkinarekonstruktio (Tarkka)" if is_live_reconstructed else "💾 Tallennehistoria"
    subtitle_html = f"<br><span style='font-size: 11px; color: #94a3b8; font-weight: normal;'>🕒 Päivitetty: {chart_updated_at} &nbsp;|&nbsp; 📅 Uusin data: {latest_data_at} &nbsp;|&nbsp; 📡 {engine_badge}</span>"

    # Build Plotly Figure
    fig = go.Figure()

    custom_data = list(zip(
        df_plot["cash_balance"],
        df_plot["total_stock_value"],
        df_plot["total_return"],
        df_plot["total_return_pct"],
    ))

    if metric_sel == "Salkun Kokonaisarvo (€)":
        latest_val = float(df_plot["total_equity"].iloc[-1])
        line_color = "#10b981" if latest_val >= starting_capital else "#f43f5e"

        fig.add_trace(go.Scatter(
            x=df_plot.index,
            y=df_plot["total_equity"],
            mode="lines+markers" if len(df_plot) <= 30 else "lines",
            name="Salkun Kokonaisarvo",
            line=dict(color=line_color, width=2.5),
            customdata=custom_data,
            hovertemplate=(
                "<b>Aika:</b> %{x|%d.%m.%Y %H:%M}<br>"
                "<b>Kokonaisarvo:</b> %{y:,.2f} €<br>"
                "<b>Käteinen:</b> %{customdata[0]:,.2f} €<br>"
                "<b>Osakesalkku:</b> %{customdata[1]:,.2f} €<br>"
                "<b>Kumulatiivinen Tuotto:</b> %{customdata[2]:+,.2f} € (%{customdata[3]:+.2f}%)"
                "<extra></extra>"
            ),
        ))

        # Benchmark line for starting capital
        fig.add_hline(
            y=starting_capital,
            line_dash="dash",
            line_color="#94a3b8",
            annotation_text=f"Lähtöpääoma ({starting_capital:,.0f} €)",
            annotation_position="bottom right",
            annotation_font_color="#94a3b8",
        )

        min_val = float(df_plot["total_equity"].min())
        max_val = float(df_plot["total_equity"].max())
        y_bottom = min(min_val, starting_capital)
        y_top = max(max_val, starting_capital)
        spread = max(y_top - y_bottom, 50.0)
        y_min = y_bottom - spread * 0.15
        y_max = y_top + spread * 0.15

        fig.update_layout(
            title=f"💼 Salkun Kokonaisarvon Kehitys ({interval_sel}){subtitle_html}",
            yaxis=dict(
                title="Euroa (€)",
                tickformat=",.0f",
                range=[y_min, y_max],
                gridcolor="#334155",
            ),
        )

    elif metric_sel == "Kokonaistuotto (€ ja %)":
        fig.add_trace(go.Scatter(
            x=df_plot.index,
            y=df_plot["total_return"],
            mode="lines+markers" if len(df_plot) <= 30 else "lines",
            name="Tuotto (€)",
            line=dict(color="#38bdf8", width=2.5),
            customdata=custom_data,
            hovertemplate=(
                "<b>Aika:</b> %{x|%d.%m.%Y %H:%M}<br>"
                "<b>Tuotto (€):</b> %{y:+,.2f} €<br>"
                "<b>Tuotto (%):</b> %{customdata[3]:+.2f}%<br>"
                "<b>Salkun arvo:</b> %{customdata[0] + customdata[1]:,.2f} €"
                "<extra></extra>"
            ),
        ))
        fig.add_hline(y=0.0, line_dash="solid", line_color="#64748b", line_width=1.5)

        min_ret = float(df_plot["total_return"].min())
        max_ret = float(df_plot["total_return"].max())
        y_bottom_r = min(min_ret, 0.0)
        y_top_r = max(max_ret, 0.0)
        spread_r = max(y_top_r - y_bottom_r, 30.0)

        fig.update_layout(
            title=f"📊 Kumulatiivinen Tuotto (€){subtitle_html}",
            yaxis=dict(
                title="Tuotto (€)",
                tickformat="+,.0f",
                range=[y_bottom_r - spread_r * 0.15, y_top_r + spread_r * 0.15],
                gridcolor="#334155",
            ),
        )

    elif metric_sel == "Varallisuuden jakautuma (Käteinen vs. Osakkeet)":
        fig.add_trace(go.Scatter(
            x=df_plot.index,
            y=df_plot["cash_balance"],
            mode="lines",
            name="Vapaa Käteinen (€)",
            line=dict(width=0.5, color="#38bdf8"),
            stackgroup="one",
            fillcolor="rgba(56, 189, 248, 0.4)",
            hovertemplate="<b>Käteinen:</b> %{y:,.2f} €<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=df_plot.index,
            y=df_plot["total_stock_value"],
            mode="lines",
            name="Osakeomistukset (€)",
            line=dict(width=0.5, color="#10b981"),
            stackgroup="one",
            fillcolor="rgba(16, 185, 129, 0.4)",
            hovertemplate="<b>Osakkeet:</b> %{y:,.2f} €<extra></extra>",
        ))
        fig.update_layout(
            title=f"🍰 Varallisuusjakauma (Käteinen vs. Osakkeet){subtitle_html}",
            yaxis=dict(title="Euroa (€)", tickformat=",.0f", gridcolor="#334155"),
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#1e293b",
        plot_bgcolor="#0f172a",
        height=390,
        margin=dict(l=60, r=30, t=55, b=30),
        xaxis=dict(gridcolor="#334155"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"🕒 **Graafi päivitetty:** {chart_updated_at} &nbsp;|&nbsp; 📅 **Uusin data:** {latest_data_at} &nbsp;|&nbsp; 📡 **Moottori:** {engine_badge}")



# ----------------------------------------------------------------------
# SIDEBAR NAVIGATION
# ----------------------------------------------------------------------
with st.sidebar:
    st.title("🐱 tradeBotTiuku")
    st.caption("Nordic & US Small-Cap Quantitative Screener v2.0")
    st.divider()

    menu = st.radio(
        "Navigation",
        [
            "💰 Paper Trading Portfolio",
            "📊 Live Screener Results",
            "👀 Turnaround Watchlist",
            "🧪 Live Ticker Scanner",
        ],
        index=0,
    )

    st.divider()

    # ------------------------------------------------------------------
    # MULTI-PORTFOLIO SELECTOR
    # ------------------------------------------------------------------
    portfolio_options = [
        "P1_Base",
        "P2_Fast_Cycle",
        "P3_Diamond_Hands",
        "P4_Institutional",
        "P5_Nordic_Only",
        "P6_US_Only",
        "P7_Deep_Value_Extreme",
        "P8_Quality_Growth",
        "P9_High_Conviction",
        "P10_Micro_Sniper",
    ]
    portfolio_meta_map = {}
    if PORTFOLIOS_CONFIG_YAML.exists():
        try:
            import yaml
            with open(PORTFOLIOS_CONFIG_YAML, "r", encoding="utf-8") as yf:
                ydata = yaml.safe_load(yf) or {}
                loaded_ports = ydata.get("portfolios", {})
                if loaded_ports:
                    portfolio_options = list(loaded_ports.keys())
                    portfolio_meta_map = loaded_ports
        except Exception as e:
            logger.debug(f"Failed to load {PORTFOLIOS_CONFIG_YAML}: {e}")

    st.subheader("📂 Paper-salkku")
    selected_portfolio = st.selectbox(
        "Valitse seurattava salkku:",
        portfolio_options,
        index=0,
        key="selected_portfolio",
        help="Valitse tarkasteltava paperisalkku 10 rinnakkaisen walk-forward-testisalkun joukosta.",
    )
    if selected_portfolio in portfolio_meta_map:
        p_meta = portfolio_meta_map[selected_portfolio]
        alloc_eur = p_meta.get("start_cash", 10000) / max(p_meta.get("slots", 10), 1)
        st.caption(
            f"🎯 **{p_meta.get('strategy', 'Profile B')}** &bull; Slots: {p_meta.get('slots', 10)} ({alloc_eur:,.0f} €/kpl)\n"
            f"- Dead Money: `{p_meta.get('dead_money_days', 180)}d`\n"
            f"- Min ADV: `{p_meta.get('min_adv', 50000):,.0f} €`\n"
            f"- Alueet: `{', '.join(p_meta.get('regions', ['FI', 'SE', 'US']))}`"
        )
        if p_meta.get("extra_filter"):
            st.caption(f"- Suodatin: `{p_meta.get('extra_filter')}`")

    st.divider()

    # Token Usage & Cost Counter (Always accessible directly under navigation)
    try:
        from core.token_tracker import get_token_stats, tracker
        token_stats = get_token_stats()
        total_tokens = token_stats.get("total_tokens", 0)
        prompt_tokens = token_stats.get("total_prompt_tokens", 0)
        completion_tokens = token_stats.get("total_completion_tokens", 0)
        total_cost = token_stats.get("total_cost_usd", 0.0)
        total_reqs = token_stats.get("total_requests", 0)

        with st.expander("🪙 Token-laskuri & Kustannus", expanded=True):
            if total_cost < 0.01:
                cost_str = f"${total_cost:.5f}"
            else:
                cost_str = f"${total_cost:.4f}"

            st.markdown(f"**Kokonaiskulutus:** `{cost_str}`")
            st.markdown(f"**Pyyntöjä tehty:** `{total_reqs} kpl`")
            st.markdown(f"**Tokenit yhteensä:** `{total_tokens:,}`")
            st.caption(f"- Syöte (Prompt): `{prompt_tokens:,}`\n- Tuotos (Output): `{completion_tokens:,}`")

            # Estimate capacity from $10 OpenRouter balance
            rem_budget = max(0.0, 10.0 - total_cost)
            avg_cost = (total_cost / total_reqs) if total_reqs > 0 else 0.00055
            rem_reports = int(rem_budget / avg_cost) if avg_cost > 0 else 18000
            st.info(f"💡 **10 $ budjetilla jäljellä:** n. **${rem_budget:.2f}** (~{rem_reports:,} analyysiä)")

            if st.button("🗑️ Nollaa laskuri", width="stretch"):
                tracker.reset_stats()
                st.success("Laskuri nollattu!")
                time.sleep(0.5)
                st.rerun()
    except Exception as e:
        logger.warning(f"Failed to render token tracker in dashboard: {e}")
        st.caption(f"⚠️ Token-laskuri: {e}")

    st.divider()
    st.subheader("⚙️ Quick Actions")

    # Refresh Data button
    if st.button("🔄 Refresh Data", width="stretch"):
        st.rerun()

    # UI Settings file persistence (stays on even across server restarts)
    UI_SETTINGS_JSON = DATA_DIR / "ui_settings.json"
    def load_ui_settings():
        if UI_SETTINGS_JSON.exists():
            try:
                with open(UI_SETTINGS_JSON, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"auto_refresh_enabled": False, "auto_refresh_interval": 30}

    def save_ui_settings(settings):
        try:
            with open(UI_SETTINGS_JSON, "w", encoding="utf-8") as f:
                json.dump(settings, f)
        except Exception:
            pass

    saved_settings = load_ui_settings()

    # Initialize session state for persistent auto-refresh
    if "auto_refresh_enabled" not in st.session_state:
        st.session_state.auto_refresh_enabled = saved_settings.get("auto_refresh_enabled", False)
    if "auto_refresh_interval" not in st.session_state:
        st.session_state.auto_refresh_interval = saved_settings.get("auto_refresh_interval", 30)

    # Auto-Refresh Toggle with persistent session state & disk persistence
    auto_refresh = st.checkbox(
        "⏱️ Automaattinen päivitys",
        value=st.session_state.auto_refresh_enabled,
        key="auto_refresh_checkbox",
        help="Päivittää näkymän ja tiedostot säännöllisesti taustalta.",
    )
    if auto_refresh != st.session_state.auto_refresh_enabled:
        st.session_state.auto_refresh_enabled = auto_refresh
        save_ui_settings({"auto_refresh_enabled": auto_refresh, "auto_refresh_interval": st.session_state.auto_refresh_interval})

    refresh_interval = st.session_state.auto_refresh_interval
    if auto_refresh:
        selected_interval = st.selectbox(
            "Päivitysväli:",
            options=[10, 30, 60, 300],
            index=[10, 30, 60, 300].index(st.session_state.auto_refresh_interval) if st.session_state.auto_refresh_interval in [10, 30, 60, 300] else 1,
            key="refresh_interval_select",
            format_func=lambda x: f"{x} sekuntia" if x < 60 else f"{x//60} minuuttia",
        )
        if selected_interval != st.session_state.auto_refresh_interval:
            st.session_state.auto_refresh_interval = selected_interval
            save_ui_settings({"auto_refresh_enabled": auto_refresh, "auto_refresh_interval": selected_interval})
        refresh_interval = selected_interval

    # Live Batch Status Check
    batch_status = get_batch_status()
    is_batch_active = bool(batch_status.get("is_running", False))

    # Mass Batch Screener Trigger inside UI
    with st.expander("🚀 Käynnistä massa-ajo (Batch Screener)", expanded=is_batch_active):
        reports_dir = DATA_DIR / "historical_reports"
        all_available_files = sorted([f for f in reports_dir.glob("*.*") if f.suffix.lower() in [".pdf", ".txt"]]) if reports_dir.exists() else []
        total_files_count = len(all_available_files)
        
        if is_batch_active:
            completed_c = batch_status.get('completed_count', 0)
            total_c = max(batch_status.get('total_count', total_files_count), 1)
            pct = min(1.0, max(0.0, completed_c / total_c))
            
            st.warning(f"⚡ **Massa-ajo on käynnissä taustalla!** (PID: `{batch_status.get('pid', 'N/A')}`)")
            st.progress(pct)
            st.markdown(f"**Edistyminen:** {completed_c} / {total_c} ({int(pct*100)}%)")
            st.caption(f"Nykyinen tiedosto: `{batch_status.get('current_file', 'Alustetaan...')}`")
            if batch_status.get('current_ticker'):
                st.caption(f"Yhtiö / Ticker: **{batch_status.get('current_ticker')}**")
                
            c_btn1, c_btn2 = st.columns(2)
            with c_btn1:
                if st.button("🔄 Päivitä tilanne", width="stretch"):
                    st.rerun()
            with c_btn2:
                if st.button("⏹️ Pysäytä massa-ajo", type="secondary", width="stretch"):
                    pid = batch_status.get("pid")
                    if pid:
                        try:
                            import subprocess
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                        except Exception:
                            pass
                    with open(BATCH_STATUS_JSON, "w", encoding="utf-8") as f:
                        json.dump({"is_running": False, "pid": 0}, f)
                    st.warning("Massa-ajo pysäytetty.")
                    time.sleep(1.0)
                    st.rerun()
        else:
            mode = st.radio(
                "Ajolaajuus:",
                [f"Kaikki raportit ({total_files_count} kpl)", "Rajoitettu erä (valitse määrä)"],
                index=0,
            )
            
            batch_limit = total_files_count
            if mode == "Rajoitettu erä (valitse määrä)":
                batch_limit = st.number_input(
                    "Käsiteltävien raporttien määrä:",
                    min_value=1,
                    max_value=max(total_files_count, 1),
                    value=min(20, max(total_files_count, 1)),
                    step=5,
                )
                
            skip_processed = st.checkbox("Ohita jo aiemmin käsitellyt raportit", value=True, help="Jos valittuna, käsittelee vain uudet raportit joita ei ole vielä data/processed_files.logissa.")
            only_latest_report = st.checkbox("Käsittele vain uusin raportti per yhtiö (Säästää tokeneita)", value=True, help="Jos valittuna, hakee kultakin osakkeelta vain tuoreimman saatavilla olevan raportin eikä analysoi vanhoja kvartaaleja turhaan.")
            enable_web_check = st.checkbox("Tee reaaliaikainen Web-tarkistus (Google News)", value=True)
            
            if st.button("▶️ Aloita massa-ajo", type="primary", width="stretch"):
                if not all_available_files:
                    st.warning("Ei raportteja kansiossa `data/historical_reports/`.")
                else:
                    import subprocess
                    cmd = [sys.executable, str(BASE_DIR / "batch_processor.py")]
                    if mode == "Rajoitettu erä (valitse määrä)":
                        cmd.extend(["--limit", str(int(batch_limit))])
                    if enable_web_check:
                        cmd.append("--web-check")
                    if not skip_processed:
                        cmd.append("--reprocess-all")
                    if not only_latest_report:
                        cmd.append("--all-historical")
                    
                    # Launch independent background process
                    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(BASE_DIR),
                        creationflags=creation_flags,
                    )
                    st.success(f"🚀 Massa-ajo käynnistetty itsenäisenä tausta-ajona (PID: {proc.pid})!")
                    time.sleep(1.2)
                    st.rerun()



    # Environment Info
    st.caption(f"📁 Workspace: `{BASE_DIR.name}`")
    try:
        fi_now = datetime.now(zoneinfo.ZoneInfo("Europe/Helsinki")).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        fi_now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    st.caption(f"🕒 Suomen aika: `{fi_now}`")


# ----------------------------------------------------------------------
# DYNAMIC FRAGMENT RENDERER (Silent in-place auto-refresh without page reload)
# ----------------------------------------------------------------------
def render_dashboard_views(active_menu: str):
    # Live Visual Pulse Badge
    try:
        now_str = datetime.now(zoneinfo.ZoneInfo("Europe/Helsinki")).strftime("%H:%M:%S")
    except Exception:
        now_str = datetime.now().strftime("%H:%M:%S")
        
    if auto_refresh:
        st.caption(f"🟢 **Live-päivitys aktiivinen** ({refresh_interval} s välein) — Viimeisin päivitys: `{now_str}`")

    # Live Batch Status Check
    status = get_batch_status()
    if status.get("is_running"):
        completed_c = status.get('completed_count', 0)
        total_c = max(status.get('total_count', 1), 1)
        pct = min(1.0, max(0.0, completed_c / total_c))
        
        st.info(
            f"⚡ **Massa-ajo on käynnissä taustalla!** (PID: `{status.get('pid', 'N/A')}`) | "
            f"Edistyminen: **{completed_c} / {total_c}** ({int(pct*100)}%) | "
            f"Käsitellään parhaillaan: **{status.get('current_ticker', '')}** (`{status.get('current_file', '')}`)"
        )
        st.progress(pct)

    # ------------------------------------------------------------------
    # VIEW 1: LIVE SCREENER RESULTS
    # ------------------------------------------------------------------
    if active_menu == "📊 Live Screener Results":
        st.markdown('<div class="main-header">📊 Dual-Lens Screener Batch Results</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Evaluates micro/small-caps through Profile A (Compounder Growth) and Profile B (Value Turnaround) with Anti-Dilution checks.</div>', unsafe_allow_html=True)

        df_results = load_csv_safely(BATCH_RESULTS_CSV, ["timestamp", "filename", "ticker", "year", "quarter", "matched_profile", "verdict", "reasoning"])

        # If empty or only header, check if random results json exists
        if df_results.empty or "verdict" not in df_results.columns or len(df_results) == 0:
            random_json = DATA_DIR / "random_20_live_web_results.json"
            if random_json.exists():
                with open(random_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    df_results = pd.DataFrame(data)

        if not df_results.empty:
            # Standardize column names
            if "verdict" not in df_results.columns:
                if "final_verdict" in df_results.columns:
                    df_results["verdict"] = df_results["final_verdict"]
                elif "doc_verdict" in df_results.columns:
                    df_results["verdict"] = df_results["doc_verdict"]
                else:
                    df_results["verdict"] = "UNKNOWN"

            if "reasoning" not in df_results.columns:
                if "doc_reasoning" in df_results.columns:
                    df_results["reasoning"] = df_results["doc_reasoning"]
                else:
                    df_results["reasoning"] = ""

            if "matched_profile" not in df_results.columns:
                df_results["matched_profile"] = "NONE"

            if "timestamp" not in df_results.columns:
                df_results["timestamp"] = datetime.now(timezone.utc).isoformat()

            # Format Finnish local time with 1-second accuracy
            df_results["Aika (Suomi)"] = df_results["timestamp"].apply(to_helsinki_time)
            # Show newest analyzed reports at the very top
            df_results = df_results.iloc[::-1].reset_index(drop=True)

        if df_results.empty or "verdict" not in df_results.columns or len(df_results) == 0:
            st.info("No batch results found in `data/batch_results.csv`. Run a screening cycle or test tickers to populate.")
        else:
            # KPI Metrics Cards
            total_eval = len(df_results)
            strong_buys = len(df_results[df_results["verdict"].astype(str).str.contains("STRONG BUY|BUY", case=False, na=False)])
            holds = len(df_results[df_results["verdict"].astype(str).str.contains("HOLD", case=False, na=False)])
            rejects = len(df_results[df_results["verdict"].astype(str).str.contains("REJECT", case=False, na=False)])
            growth_count = len(df_results[df_results["matched_profile"].astype(str).str.contains("GROWTH", case=False, na=False)])
            value_count = len(df_results[df_results["matched_profile"].astype(str).str.contains("VALUE", case=False, na=False)])

            col1, col2, col3, col4, col5 = st.columns(5)
            with col1:
                st.metric("Total Analyzed", total_eval)
            with col2:
                st.metric("🔥 Strong Buys", strong_buys)
            with col3:
                st.metric("🟡 Holds", holds)
            with col4:
                st.metric("❌ Rejections", rejects)
            with col5:
                st.metric("Growth / Value", f"{growth_count} / {value_count}")

            st.divider()

            # Filters Bar
            fcol1, fcol2, fcol3 = st.columns([2, 1, 1])
            with fcol1:
                search_query = st.text_input("🔍 Search by Ticker / Company Name:", placeholder="e.g. GOOG, ESCA, QTCOM...")
            with fcol2:
                verdict_filter = st.selectbox("Filter Verdict:", ["All", "STRONG BUY", "HOLD", "REJECT", "WATCH_TURNAROUND"])
            with fcol3:
                market_filter = st.selectbox("Market Universe:", ["All Markets", "🇺🇸 US Micro-Caps", "🇫🇮 / 🇸🇪 Nordics"])

            # Apply Filtering
            filtered_df = df_results.copy()
            if search_query:
                filtered_df = filtered_df[
                    filtered_df["ticker"].astype(str).str.contains(search_query, case=False, na=False) |
                    filtered_df.get("reasoning", pd.Series([""]*len(filtered_df))).astype(str).str.contains(search_query, case=False, na=False)
                ]
            if verdict_filter != "All":
                filtered_df = filtered_df[filtered_df["verdict"].astype(str).str.contains(verdict_filter, case=False, na=False)]
            if market_filter == "🇺🇸 US Micro-Caps":
                filtered_df = filtered_df[~filtered_df["ticker"].astype(str).str.contains(r"\.(HE|ST|OL|CO)", regex=True, na=False)]
            elif market_filter == "🇫🇮 / 🇸🇪 Nordics":
                filtered_df = filtered_df[filtered_df["ticker"].astype(str).str.contains(r"\.(HE|ST|OL|CO)", regex=True, na=False)]

            st.caption(f"Showing {len(filtered_df)} of {total_eval} records")

            # Modern, Clean Interactive Results List (Clicking opens Deep-Dive directly)
            st.markdown("### 📋 Analysoidut Raportit & Syventävät Selvitykset")
            st.caption("💡 *Klikkaa mitä tahansa yhtiötä listalta avataksesi suoraan sen täydellisen AI-analyysin, tasetiedot ja uutistarkistukset.*")

            # Simple Pagination / Display Limit
            page_size = 30
            num_pages = max(1, (len(filtered_df) + page_size - 1) // page_size)
            if num_pages > 1:
                cur_page = st.number_input("Sivu:", min_value=1, max_value=num_pages, value=1, step=1)
                start_i = (cur_page - 1) * page_size
                page_df = filtered_df.iloc[start_i : start_i + page_size]
            else:
                page_df = filtered_df

            for idx, row in page_df.iterrows():
                t_name = str(row.get("ticker", "N/A")).strip()
                v_text = str(row.get("verdict", "N/A")).strip()
                p_text = str(row.get("matched_profile", "NONE")).strip()
                time_fi = to_helsinki_time(row.get("timestamp"))
                file_name = row.get("filename", row.get("file", "N/A"))
                reason_text = row.get("reasoning", row.get("doc_reasoning", "Ei perusteluita saatavilla."))
                web_text = row.get("web_reasoning")

                # Visual emoji indicator based on verdict
                icon = "🔥" if "STRONG BUY" in v_text or "BUY" in v_text else ("👀" if "WATCH_TURNAROUND" in v_text else ("🟡" if "HOLD" in v_text else "❌"))
                header_title = f"{icon} **{t_name}** — `{v_text}` | *{p_text}* | 🕒 {time_fi}"

                with st.expander(header_title, expanded=False):
                    st.markdown(f"**Raporttitiedosto:** `{file_name}` | **Aika (Suomi):** `{time_fi}`")
                    st.markdown("##### 🧠 Pedagogiset AI-Perustelut:")
                    st.info(reason_text)
                    if pd.notna(web_text) and str(web_text).strip():
                        st.markdown("##### 🌐 Reaaliaikainen Web-Uutistarkistus:")
                        st.success(str(web_text).strip())

    # ------------------------------------------------------------------
    # VIEW 2: TURNAROUND WATCHLIST
    # ------------------------------------------------------------------
    elif active_menu == "👀 Turnaround Watchlist":
        st.markdown('<div class="main-header">👀 Turnaround Watchlist (`WATCH_TURNAROUND`)</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Identifies divergence between lagging historical financial reports and real-time positive news catalysts.</div>', unsafe_allow_html=True)

        df_watch = load_csv_safely(WATCHLIST_CSV, [
            "timestamp", "company_name", "ticker", "strategy_type", "title",
            "link", "doc_verdict", "doc_reasoning", "positive_catalysts_found", "web_reasoning"
        ])

        if df_watch.empty:
            df_batch = load_csv_safely(BATCH_RESULTS_CSV)
            if not df_batch.empty and "verdict" in df_batch.columns:
                watch_rows = df_batch[df_batch["verdict"].astype(str).str.contains("WATCH_TURNAROUND", case=False, na=False)]
                if not watch_rows.empty:
                    df_watch = watch_rows.copy()

        if df_watch.empty:
            random_json = DATA_DIR / "random_20_live_web_results.json"
            if random_json.exists():
                with open(random_json, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    turnarounds = [r for r in raw if "WATCH_TURNAROUND" in str(r.get("final_verdict", ""))]
                    if turnarounds:
                        df_watch = pd.DataFrame(turnarounds)

        if df_watch.empty:
            st.info("No turnaround opportunities currently in `data/watchlist_turnarounds.csv`. Candidates will populate automatically when lagging reports diverge from strong positive real-time news.")
        else:
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Watchlist Turnarounds", len(df_watch))
            with col2:
                st.metric("Trading Status", "👀 Watchlist Only (No Auto-Buy)")
            with col3:
                latest_cand = df_watch.iloc[-1].get("ticker", "N/A")
                st.metric("Latest Candidate", latest_cand)

            st.divider()

            for idx, row in df_watch.iterrows():
                ticker = row.get("ticker", "UNKNOWN")
                comp = row.get("company_name", ticker)
                ts = to_helsinki_time(row.get("timestamp"))
                catalysts = row.get("positive_catalysts_found", row.get("catalysts_found", "Turnaround catalysts detected"))
                if isinstance(catalysts, list):
                    catalysts_str = ", ".join(catalysts) if catalysts else "Operational Turnaround / Sales Beat"
                else:
                    catalysts_str = str(catalysts)

                with st.expander(f"📌 **{ticker}** ({comp}) — *{catalysts_str}* | 🕒 {ts}", expanded=False):
                    c_left, c_right = st.columns(2)
                    with c_left:
                        st.markdown("#### ⚠️ Step 1: Historical Financial Lag (10-Q / PR)")
                        st.caption(f"Initial Verdict: `{row.get('doc_verdict', 'HOLD / REJECT')}`")
                        st.warning(row.get("doc_reasoning", "Historical losses or low margins reported in past quarter."))
                    with c_right:
                        st.markdown("#### 🚀 Step 2: Real-Time Positive Catalysts (Web / News)")
                        st.caption(f"Catalysts: `{catalysts_str}`")
                        st.success(row.get("web_reasoning", "Live search confirms positive restructuring, sales beat, or contract win."))

                    b_col1, b_col2 = st.columns([4, 1])
                    with b_col1:
                        if row.get("link"):
                            st.markdown(f"[🔗 Avaa tiedote / uutisartikkeli]({row['link']})")
                    with b_col2:
                        if st.button("🗑️ Poista listalta", key=f"del_watch_{ticker}_{idx}"):
                            if remove_from_turnaround_watchlist(WATCHLIST_CSV, ticker):
                                st.success(f"Poistettu {ticker} seurantalistalta.")
                                st.rerun()

    # ------------------------------------------------------------------
    # VIEW 3: PAPER TRADING PORTFOLIO & RISK CONTROLS
    # ------------------------------------------------------------------
    elif active_menu == "💰 Paper Trading Portfolio":
        st.markdown('<div class="main-header">💰 Virtual Paper Trading Portfolio & Risk Controls</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Multi-Portfolio Walk-Forward Live Testing, parameterized position sizing, and Tri-Layer Exit enforcement.</div>', unsafe_allow_html=True)

        selected_portfolio = st.session_state.get("selected_portfolio", "P1_Base")
        port_state_file = PORTFOLIOS_DIR / f"portfolio_{selected_portfolio}_state.json"
        port_history_csv = PORTFOLIOS_DIR / f"portfolio_{selected_portfolio}_history.csv"
        port_equity_json = PORTFOLIOS_DIR / f"portfolio_{selected_portfolio}_history.json"
        active_equity_file = port_equity_json if port_equity_json.exists() else PORTFOLIO_HISTORY_JSON

        # Display portfolio metadata card
        portfolio_meta = {}
        if PORTFOLIOS_CONFIG_YAML.exists():
            try:
                import yaml
                with open(PORTFOLIOS_CONFIG_YAML, "r", encoding="utf-8") as yf:
                    ydata = yaml.safe_load(yf) or {}
                    portfolio_meta = ydata.get("portfolios", {}).get(selected_portfolio, {})
            except Exception:
                pass

        if portfolio_meta:
            alloc_eur = portfolio_meta.get("start_cash", 10000) / max(portfolio_meta.get("slots", 10), 1)
            filter_note = f" &bull; Extra: `{portfolio_meta['extra_filter']}`" if portfolio_meta.get("extra_filter") else ""
            st.info(
                f"💼 **Aktiivinen salkku: `{selected_portfolio}`** | "
                f"Strategia: **{portfolio_meta.get('strategy', 'Profile B')}** | "
                f"Slots: **{portfolio_meta.get('slots', 10)}** ({alloc_eur:,.0f} €/osto) | "
                f"Dead Money: **{portfolio_meta.get('dead_money_days', 180)}d** | "
                f"Min ADV: **{portfolio_meta.get('min_adv', 50000):,.0f} €** | "
                f"Alueet: **{', '.join(portfolio_meta.get('regions', ['FI', 'SE', 'US']))}**{filter_note}"
            )

        tab1, tab_inst, tab2, tab3, tab4, tab5 = st.tabs([
            "📌 Avoimet Positiot & Trailing Stopit",
            "🏛️ Institutional Analytics",
            "⚡ Riskinhallinta & Testiosto",
            "📜 Toteutuneet Kaupat (Trade History)",
            "🔔 Hälytysloki (Screener Alerts)",
            "📊 Salkkuvertailu (Kaikki)",
        ])

        with tab1:
            starting_capital = 10_000.0
            free_cash = 10_000.0
            raw_positions = []

            if port_state_file.exists():
                try:
                    with open(port_state_file, "r", encoding="utf-8") as f_acc:
                        acc_data = json.load(f_acc)
                        starting_capital = float(acc_data.get("starting_balance", 10_000.0))
                        free_cash = float(acc_data.get("cash_balance", 10_000.0))
                        raw_positions = acc_data.get("positions", [])
                except Exception:
                    pass
                if raw_positions:
                    df_pos = pd.DataFrame(raw_positions)
                    df_pos.columns = [str(c).strip().lower() for c in df_pos.columns]
                else:
                    df_pos = pd.DataFrame()
            else:
                # Portfolio state file not yet created (daemon hasn't run yet).
                # Show the portfolio as empty — do NOT fall back to the shared
                # legacy paper_account.json / open_positions.csv which would make
                # every portfolio show the same data.
                start_cash = portfolio_meta.get("start_cash", 10_000.0) if portfolio_meta else 10_000.0
                starting_capital = start_cash
                free_cash = start_cash
                df_pos = pd.DataFrame()
                st.warning(
                    f"⏳ Salkun `{selected_portfolio}` tiedostoa ei löydy vielä — "
                    "daemon luo sen ensimmäisessä ajossaan. Käynnistä daemon tai odota seuraavaa ajastettua sykliä."
                )

            if df_pos.empty:
                st.info(f"Ei avoimia paperipositioita salkussa `{selected_portfolio}`.")
                render_portfolio_equity_chart(
                    df_pos=df_pos,
                    free_cash=free_cash,
                    starting_capital=starting_capital,
                    history_file=active_equity_file,
                )
            else:

                # Extract tickers and fetch live market quotes
                tickers_list = []
                for _, r in df_pos.iterrows():
                    t_val = str(r.get("ticker", "")).strip().upper()
                    if t_val:
                        tickers_list.append(t_val)

                live_quotes = fetch_positions_live_data(tuple(tickers_list))

                # Build enriched position records
                enriched_rows = []
                total_invested = 0.0
                total_market_val = 0.0
                needs_healing = False
                healed_rows_to_save = []

                fx_rates = get_live_fx_rates()
                fx_eur_usd = fx_rates.get("EURUSD", 1.15)
                fx_eur_sek = fx_rates.get("EURSEK", 11.30)

                for _, r in df_pos.iterrows():
                    t_sym = str(r.get("ticker", "")).strip().upper()
                    if not t_sym:
                        continue
                    
                    buy_date = (
                        r.get("entrydate")
                        or r.get("entry_date")
                        or r.get("buy date")
                        or r.get("buy_date")
                        or r.get("date")
                        or "2026-09-18"
                    )
                    shares = float(pd.to_numeric(r.get("shares", 0.0), errors="coerce") or 0.0)
                    cap_invested = float(
                        pd.to_numeric(
                            r.get("positionvalue", r.get("position_value", r.get("capital invested", r.get("capital_invested", 0.0)))),
                            errors="coerce",
                        )
                        or 0.0
                    )
                    buy_price = float(
                        pd.to_numeric(
                            r.get("entryprice", r.get("entry_price", r.get("buy price", r.get("buy_price", 0.0)))),
                            errors="coerce",
                        )
                        or 0.0
                    )
                    # Auto-heal: If buy_price <= 0, recover from KNOWN_ENTRY_PRICES or current_price from CSV
                    if buy_price <= 0.0:
                        needs_healing = True
                        if t_sym in KNOWN_ENTRY_PRICES:
                            buy_price = KNOWN_ENTRY_PRICES[t_sym]
                        else:
                            fallback_csv_price = float(pd.to_numeric(r.get("current_price", r.get("currentprice", r.get("highest_price_seen", 0.0))), errors="coerce") or 0.0)
                            if fallback_csv_price > 0.0:
                                buy_price = fallback_csv_price

                    if buy_price <= 0.0 and cap_invested > 0.0 and shares > 0.0:
                        buy_price = round(cap_invested / shares, 4)
                    if cap_invested <= 0.0 and buy_price > 0.0 and shares > 0.0:
                        cap_invested = round(buy_price * shares, 2)

                    strategy = r.get("strategy_type", r.get("strategy", "SATELLITE"))

                    q = live_quotes.get(t_sym, {})
                    curr_price = float(q.get("current_price") or buy_price)
                    day_chg = float(q.get("day_change_pct") or 0.0)

                    # Infer correct currency based on ticker suffix
                    curr_curr = q.get("currency") or r.get("currency")
                    if not curr_curr or curr_curr == "USD":
                        if t_sym.endswith(".HE"):
                            curr_curr = "EUR"
                        elif t_sym.endswith(".ST"):
                            curr_curr = "SEK"
                        elif t_sym.endswith(".OL"):
                            curr_curr = "NOK"
                        elif t_sym.endswith(".CO"):
                            curr_curr = "DKK"
                        else:
                            curr_curr = "USD"

                    if r.get("currency") != curr_curr or float(r.get("buy_price", 0.0) or 0.0) <= 0.0:
                        needs_healing = True

                    healed_rows_to_save.append({
                        "ticker": t_sym,
                        "market": "FI" if t_sym.endswith(".HE") else ("SE" if t_sym.endswith(".ST") else "US"),
                        "buy_date": buy_date,
                        "buy_price": round(buy_price, 4),
                        "current_price": round(curr_price, 4),
                        "highest_price_seen": round(max(buy_price, curr_price, float(r.get("highest_price_seen", 0.0) or 0.0)), 4),
                        "catastrophic_stop": round(buy_price * 0.50, 4),
                        "shares": shares,
                        "currency": curr_curr,
                        "last_evaluated_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    })

                    # FX conversion to EUR
                    if curr_curr == "EUR":
                        fx_to_eur = 1.0
                    elif curr_curr == "USD":
                        fx_to_eur = 1.0 / fx_eur_usd
                    elif curr_curr == "SEK":
                        fx_to_eur = 1.0 / fx_eur_sek
                    else:
                        fx_to_eur = 1.0

                    pos_mkt_val = shares * curr_price
                    pos_pnl_abs = pos_mkt_val - cap_invested
                    pos_pnl_pct = ((curr_price - buy_price) / buy_price * 100.0) if buy_price > 0 else 0.0
                    cat_stop = buy_price * 0.50
                    stop_dist_pct = ((curr_price - cat_stop) / curr_price * 100.0) if curr_price > 0 else 0.0

                    invested_eur = cap_invested * fx_to_eur
                    market_val_eur = pos_mkt_val * fx_to_eur
                    pnl_abs_eur = pos_pnl_abs * fx_to_eur

                    total_invested += invested_eur
                    total_market_val += market_val_eur

                    enriched_rows.append({
                        "ticker": t_sym,
                        "buy_date": buy_date,
                        "buy_price": buy_price,
                        "curr_price": curr_price,
                        "day_change_pct": day_chg,
                        "shares": shares,
                        "cap_invested": cap_invested,
                        "market_value": pos_mkt_val,
                        "pnl_abs": pos_pnl_abs,
                        "pnl_pct": pos_pnl_pct,
                        "invested_eur": invested_eur,
                        "market_val_eur": market_val_eur,
                        "pnl_abs_eur": pnl_abs_eur,
                        "cat_stop": cat_stop,
                        "stop_dist_pct": stop_dist_pct,
                        "strategy": strategy,
                        "currency": curr_curr,
                    })

                df_enriched = pd.DataFrame(enriched_rows)

                # Persist healed positions to disk if repairs were made
                if needs_healing and healed_rows_to_save:
                    if port_state_file.exists():
                        try:
                            with open(port_state_file, "r", encoding="utf-8") as f_acc:
                                acc_save = json.load(f_acc)
                            acc_save["positions"] = healed_rows_to_save
                            with open(port_state_file, "w", encoding="utf-8") as f_acc:
                                json.dump(acc_save, f_acc, indent=2, ensure_ascii=False)
                        except Exception:
                            pass
                    try:
                        pd.DataFrame(healed_rows_to_save).to_csv(OPEN_POSITIONS_CSV, index=False)
                    except Exception:
                        pass

                # Portfolio KPI calculations (Normalized to EUR)
                unrealized_pnl = total_market_val - total_invested
                unrealized_pnl_pct = (unrealized_pnl / total_invested * 100.0) if total_invested > 0 else 0.0
                total_equity = free_cash + total_market_val
                portfolio_total_return = total_equity - starting_capital
                portfolio_total_return_pct = (portfolio_total_return / starting_capital * 100.0) if starting_capital > 0 else 0.0

                # Automatically record continuous equity snapshot for history tracking
                record_portfolio_snapshot(
                    total_equity=total_equity,
                    cash_balance=free_cash,
                    stock_value=total_market_val,
                    starting_balance=starting_capital,
                    history_file=active_equity_file,
                    has_positions=True,
                )

                win_count = sum(1 for row in enriched_rows if row["pnl_pct"] > 0)
                loss_count = sum(1 for row in enriched_rows if row["pnl_pct"] < 0)
                win_rate = (win_count / len(enriched_rows) * 100.0) if enriched_rows else 0.0
                avg_ret = df_enriched["pnl_pct"].mean() if not df_enriched.empty else 0.0
                med_ret = df_enriched["pnl_pct"].median() if not df_enriched.empty else 0.0

                best_stock_label = "-"
                best_stock_delta = None
                worst_stock_label = "-"
                worst_stock_delta = None
                if not df_enriched.empty:
                    b_row = df_enriched.loc[df_enriched["pnl_pct"].idxmax()]
                    w_row = df_enriched.loc[df_enriched["pnl_pct"].idxmin()]
                    best_stock_label = f"{b_row['ticker']} ({b_row['pnl_pct']:+.2f}%)"
                    best_stock_delta = f"{b_row['pnl_abs_eur']:+.2f} €"
                    worst_stock_label = f"{w_row['ticker']} ({w_row['pnl_pct']:+.2f}%)"
                    worst_stock_delta = f"{w_row['pnl_abs_eur']:+.2f} €"

                # Render Top KPI Cards (Row 1: Salkun Arvo & PnL)
                kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
                with kpi1:
                    st.metric(
                        "Salkun Kokonaisarvo",
                        f"{total_equity:,.2f} €",
                        f"{portfolio_total_return:+,.2f} € ({portfolio_total_return_pct:+.2f}%)",
                    )
                with kpi2:
                    st.metric("Sijoitettu Pääoma", f"{total_invested:,.2f} €")
                with kpi3:
                    st.metric(
                        "Avoin Tuotto (PnL)",
                        f"{unrealized_pnl:+,.2f} €",
                        f"{unrealized_pnl_pct:+.2f}%",
                    )
                with kpi4:
                    st.metric("Vapaa Käteinen", f"{free_cash:,.2f} €")
                with kpi5:
                    st.metric(
                        "Avoimet Positiot",
                        f"{len(df_enriched)} kpl",
                        f"{win_count} 🟢 / {loss_count} 🔴",
                    )

                # Render Top KPI Cards (Row 2: Tilastollinen Yhteenveto)
                stat1, stat2, stat3, stat4, stat5 = st.columns(5)
                with stat1:
                    st.metric("Voitolliset (Win Rate)", f"{win_rate:.1f}%", f"{win_count} / {len(df_enriched)} kpl")
                with stat2:
                    st.metric("Keskimääräinen Tuotto", f"{avg_ret:+.2f}%")
                with stat3:
                    st.metric("Mediaanituotto", f"{med_ret:+.2f}%")
                with stat4:
                    st.metric("Paras Positio", best_stock_label, best_stock_delta)
                with stat5:
                    st.metric("Heikoin Positio", worst_stock_label, worst_stock_delta)

                st.divider()

                # Visual 0: Interactive Portfolio Total Equity Performance Chart
                render_portfolio_equity_chart(
                    df_pos=df_pos,
                    free_cash=free_cash,
                    starting_capital=starting_capital,
                    history_file=active_equity_file,
                )

                st.divider()

                # Action buttons row
                act_col1, act_col2, act_col3 = st.columns([2, 1, 1])
                with act_col1:
                    st.markdown("##### 📈 Reaaliaikainen Osakekehitys & Tri-Layer Stopit")
                    st.caption(f"Kurssit noudetaan suoraan markkinalta. Valuutat muunnettu euroiksi (1 EUR = {fx_eur_usd:.2f} $ | 1 EUR = {fx_eur_sek:.2f} SEK).")
                with act_col2:
                    if st.button("🔄 Päivitä Kurssit Nyt", width="stretch"):
                        st.cache_data.clear()
                        st.success("Reaaliaikaiset kurssit päivitetty!")
                        time.sleep(0.5)
                        st.rerun()
                with act_col3:
                    if st.button("🛡️ Tarkista Trailing Stopit", type="primary", width="stretch"):
                        from screener.portfolio_manager import update_trailing_stops
                        with st.spinner("Tarkistetaan exit-säännöt ja stopit..."):
                            res = update_trailing_stops()
                        st.success(f"Tarkistettu! Avoimia: {res.get('updated_positions', 0)}, Suljettu: {res.get('closed_positions', 0)}")
                        time.sleep(1.0)
                        st.rerun()

                # ------------------------------------------------------------------
                # Visual 1: Salkun Osakkeiden Tuottovertailu (Bar Chart)
                # ------------------------------------------------------------------
                if not df_enriched.empty:
                    df_sorted = df_enriched.sort_values(by="pnl_pct", ascending=True)
                    bar_colors = ["#10b981" if p >= 0 else "#f43f5e" for p in df_sorted["pnl_pct"]]
                    bar_updated_at = datetime.now(HELSINKI_TZ).strftime("%d.%m.%Y %H:%M:%S")
                    bar_latest_data_at = live_quotes.get("_fetch_timestamp") or bar_updated_at
                    bar_subtitle = f"<br><span style='font-size: 11px; color: #94a3b8; font-weight: normal;'>🕒 Päivitetty: {bar_updated_at} &nbsp;|&nbsp; 📅 Uusin data: {bar_latest_data_at}</span>"

                    fig_bar = go.Figure(go.Bar(
                        x=df_sorted["pnl_pct"],
                        y=df_sorted["ticker"],
                        orientation="h",
                        marker=dict(color=bar_colors),
                        text=[f"{p:+.2f}%" for p in df_sorted["pnl_pct"]],
                        textposition="outside",
                        hovertemplate="<b>%{y}</b><br>Tuotto: %{x:+.2f}%<extra></extra>",
                    ))
                    fig_bar.update_layout(
                        title=f"📊 Salkun Osakkeiden Tuotto Ostohetkestä (%){bar_subtitle}",
                        template="plotly_dark",
                        paper_bgcolor="#1e293b",
                        plot_bgcolor="#0f172a",
                        height=340,
                        margin=dict(l=90, r=40, t=55, b=30),
                        xaxis=dict(title="Tuotto %", zeroline=True, zerolinecolor="#64748b", zerolinewidth=1.5),
                        yaxis=dict(title=""),
                    )
                    st.plotly_chart(fig_bar, use_container_width=True)
                    st.caption(f"🕒 **Graafi päivitetty:** {bar_updated_at} &nbsp;|&nbsp; 📅 **Uusin data:** {bar_latest_data_at}")


                # ------------------------------------------------------------------
                # Visual 2: Formatted Portfolio Table
                # ------------------------------------------------------------------
                st.markdown("#### 📋 Avoimet Positiot ja Tuloserittely")
                
                table_cols = [
                    "ticker", "currency", "buy_date", "buy_price", "curr_price", "day_change_pct",
                    "shares", "invested_eur", "market_val_eur", "pnl_abs_eur", "pnl_pct",
                    "cat_stop", "stop_dist_pct", "strategy"
                ]
                existing_cols = [c for c in table_cols if c in df_enriched.columns]
                table_display = df_enriched[existing_cols].copy()

                # Ensure numeric types remain numeric for proper column sorting
                num_cols = ["buy_price", "curr_price", "day_change_pct", "shares", "invested_eur", "market_val_eur", "pnl_abs_eur", "pnl_pct", "cat_stop", "stop_dist_pct"]
                for c in num_cols:
                    if c in table_display.columns:
                        table_display[c] = pd.to_numeric(table_display[c], errors="coerce").fillna(0.0)

                if "buy_date" in table_display.columns:
                    table_display["buy_date"] = pd.to_datetime(table_display["buy_date"], errors="coerce").dt.date

                col_renames = {
                    "ticker": "Ticker",
                    "currency": "Valuutta",
                    "buy_date": "Ostopäivä",
                    "buy_price": "Ostohinta",
                    "curr_price": "Nykykurssi",
                    "day_change_pct": "Päivämuutos",
                    "shares": "Kpl",
                    "invested_eur": "Hankinta-arvo (€)",
                    "market_val_eur": "Markkina-arvo (€)",
                    "pnl_abs_eur": "Tulos (€)",
                    "pnl_pct": "Tuotto %",
                    "cat_stop": "Stop-loss (-50%)",
                    "stop_dist_pct": "Puskuri stoppiin",
                    "strategy": "Strategia",
                }
                table_display = table_display.rename(columns=col_renames)

                col_config = {
                    "Ticker": st.column_config.TextColumn("Ticker"),
                    "Valuutta": st.column_config.TextColumn("Valuutta", help="Alkuperäinen noteerausvaluutta"),
                    "Ostopäivä": st.column_config.DateColumn("Ostopäivä", format="YYYY-MM-DD"),
                    "Ostohinta": st.column_config.NumberColumn("Ostohinta", format="%,.2f", help="Ostohinta alkuperäisessä valuutassa"),
                    "Nykykurssi": st.column_config.NumberColumn("Nykykurssi", format="%,.2f", help="Viimeisin kurssinoteeraus"),
                    "Päivämuutos": st.column_config.NumberColumn("Päivämuutos", format="%+,.2f %%", help="Päiväkohtainen kurssimuutos prosentteina"),
                    "Kpl": st.column_config.NumberColumn("Kpl", format="%,.0f", help="Osakemäärä"),
                    "Hankinta-arvo (€)": st.column_config.NumberColumn("Hankinta-arvo (€)", format="%,.2f €", help="Hankinta-arvo euroissa"),
                    "Markkina-arvo (€)": st.column_config.NumberColumn("Markkina-arvo (€)", format="%,.2f €", help="Markkina-arvo euroissa"),
                    "Tulos (€)": st.column_config.NumberColumn("Tulos (€)", format="%+,.2f €", help="Avoin tulos euroina"),
                    "Tuotto %": st.column_config.NumberColumn("Tuotto %", format="%+,.2f %%", help="Tuotto ostohinnasta prosentteina"),
                    "Stop-loss (-50%)": st.column_config.NumberColumn("Stop-loss (-50%)", format="%,.2f", help="Katastrofistoppi (-50%) alkuperäisessä valuutassa"),
                    "Puskuri stoppiin": st.column_config.NumberColumn("Puskuri stoppiin", format="%,.1f %%", help="Etäisyys nykykurssista stoppiin"),
                    "Strategia": st.column_config.TextColumn("Strategia"),
                }

                st.dataframe(
                    table_display,
                    column_config=col_config,
                    width="stretch",
                    hide_index=True,
                )

                st.divider()

                # ------------------------------------------------------------------
                # Visual 3: Interactive Stock Price Development Chart
                # ------------------------------------------------------------------
                st.markdown("### 📈 Osakkeen Kehityskäyrä ja Tasot (Interactive Stock Inspector)")
                st.caption("Tarkastele minkä tahansa salkun osakkeen kurssikehitystä, ostonoteerausta ja suojaavia stop-loss -tasoja.")

                c_sel1, c_sel2 = st.columns([2, 1])
                with c_sel1:
                    ticker_choices = [
                        f"{row['ticker']} ({row['pnl_pct']:+.2f}%)"
                        for row in enriched_rows
                    ]
                    selected_label = st.selectbox("Valitse tarkasteltava osake:", ticker_choices, index=0)
                    selected_ticker = selected_label.split()[0]
                with c_sel2:
                    period_choice = st.selectbox("Aikaväli:", ["1mo", "3mo", "6mo", "1y", "ytd"], index=1)

                # Find selected stock info
                pos_match = next((row for row in enriched_rows if row["ticker"] == selected_ticker), None)

                if pos_match:
                    with st.spinner(f"Noudetaan historiadataa osakkeelle {selected_ticker}..."):
                        df_hist, resolved_ticker = fetch_ticker_history(selected_ticker, period=period_choice)

                    if df_hist.empty:
                        st.warning(f"Ei historiadataa saatavilla osakkeelle {selected_ticker}.")
                    else:
                        fig_stock = go.Figure()

                        # Price Line
                        fig_stock.add_trace(go.Scatter(
                            x=df_hist.index,
                            y=df_hist["Close"],
                            mode="lines",
                            name=f"{selected_ticker} Päätöskurssi",
                            line=dict(color="#38bdf8", width=2.5),
                        ))

                        # Buy Price Line
                        b_price = pos_match["buy_price"]
                        fig_stock.add_hline(
                            y=b_price,
                            line_dash="dash",
                            line_color="#10b981",
                            line_width=2,
                            annotation_text=f"Ostotaso: {b_price:,.2f} {pos_match['currency']}",
                            annotation_position="top left",
                            annotation_font_color="#10b981",
                        )

                        # Stop-loss Line
                        s_price = pos_match["cat_stop"]
                        fig_stock.add_hline(
                            y=s_price,
                            line_dash="dash",
                            line_color="#f43f5e",
                            line_width=2,
                            annotation_text=f"Katastrofistop (-50%): {s_price:,.2f} {pos_match['currency']}",
                            annotation_position="bottom left",
                            annotation_font_color="#f43f5e",
                        )

                        stock_updated_at = datetime.now(HELSINKI_TZ).strftime("%d.%m.%Y %H:%M:%S")
                        latest_bar = df_hist.index.max()
                        if hasattr(latest_bar, "tzinfo") and latest_bar.tzinfo is not None:
                            try:
                                latest_bar_local = latest_bar.tz_convert(HELSINKI_TZ)
                            except Exception:
                                latest_bar_local = latest_bar
                        else:
                            latest_bar_local = latest_bar

                        if hasattr(latest_bar_local, "hour") and (latest_bar_local.hour != 0 or latest_bar_local.minute != 0):
                            stock_latest_data_at = latest_bar_local.strftime("%d.%m.%Y %H:%M:%S")
                        elif hasattr(latest_bar_local, "strftime"):
                            stock_latest_data_at = latest_bar_local.strftime("%d.%m.%Y")
                        else:
                            stock_latest_data_at = str(latest_bar_local)

                        stock_subtitle = f"<br><span style='font-size: 11px; color: #94a3b8; font-weight: normal;'>🕒 Päivitetty: {stock_updated_at} &nbsp;|&nbsp; 📅 Uusin data: {stock_latest_data_at}</span>"

                        fig_stock.update_layout(
                            title=f"📈 {selected_ticker} ({resolved_ticker}) — Kurssikehitys ja Positiotasot{stock_subtitle}",
                            template="plotly_dark",
                            paper_bgcolor="#1e293b",
                            plot_bgcolor="#0f172a",
                            height=430,
                            margin=dict(l=50, r=40, t=65, b=40),
                            xaxis=dict(title="Päivämäärä", showgrid=True, gridcolor="#334155"),
                            yaxis=dict(title=f"Kurssi ({pos_match['currency']})", showgrid=True, gridcolor="#334155"),
                            hovermode="x unified",
                        )
                        st.plotly_chart(fig_stock, use_container_width=True)
                        st.caption(f"🕒 **Graafi päivitetty:** {stock_updated_at} &nbsp;|&nbsp; 📅 **Uusin data:** {stock_latest_data_at}")


                        # Metrics under chart
                        m1, m2, m3, m4 = st.columns(4)
                        with m1:
                            st.metric("Ostopäivä", str(pos_match["buy_date"]))
                        with m2:
                            st.metric(
                                "Ostohinta vs Nykykurssi",
                                f"{pos_match['curr_price']:,.2f} {pos_match['currency']}",
                                f"Ostettu: {pos_match['buy_price']:,.2f}",
                            )
                        with m3:
                            st.metric(
                                "Tuotto Ostohetkestä",
                                f"{pos_match['pnl_pct']:+.2f}%",
                                f"${pos_match['pnl_abs']:+,.2f}",
                            )
                        with m4:
                            st.metric(
                                "Puskuri Stoppiin (-50%)",
                                f"{pos_match['stop_dist_pct']:.1f}%",
                                f"Stop: {pos_match['cat_stop']:,.2f}",
                            )

        # ------------------------------------------------------------------
        # TAB: INSTITUTIONAL ANALYTICS
        # ------------------------------------------------------------------
        with tab_inst:
            st.markdown("### 🏛️ Institutional Quantitative & Operational Analytics")
            st.caption(
                "Pääomarahastotason riskikorjatut tunnusluvut, usean hypoteesin DSR-korjaus (Multiple Testing Correction), "
                "kohinaton viikkotuottojen korrelaatiomatriisi, markkinaregiimiseuranta ja botin operatiiviset terveysmittarit."
            )

            # ------------------------------------------------------------------
            # 1. Controls & Noise Reduction Toggle (Evaluated First)
            # ------------------------------------------------------------------
            col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 2, 1])
            with col_ctrl1:
                weekly_smoothing = st.toggle(
                    "🗓️ Viikkoaggregointi (Weekly Smoothing)",
                    value=True,
                    help="Aggregoi salkkujen pääoman viikkotasolle (perjantain sulkemiset) päivittäisen mikroheilahtelun ja satunnaiskohinan poistamiseksi.",
                )
            with col_ctrl2:
                roll_window = st.select_slider(
                    "Rullaava analyysi-ikkuna:",
                    options=[14, 30, 60, 90],
                    value=30,
                    help="Päivien määrä rullaavalle Sharpen ja Sortinon laskennalle (oletus: 30 päivää).",
                )
            with col_ctrl3:
                if st.button("🔄 Laske uudelleen", width="stretch", key="btn_recalc_quant"):
                    st.rerun()

            # ------------------------------------------------------------------
            # 2. Calculation via quant_analytics Engine
            # ------------------------------------------------------------------
            inst_data = qa.generate_institutional_summary(
                PORTFOLIOS_DIR,
                weekly_smoothing=weekly_smoothing,
                rolling_window=roll_window,
            )
            df_summary = inst_data["summary_df"]
            corr_matrix = inst_data["correlation_matrix"]
            dsr_report = inst_data["dsr_report"]
            regime = inst_data.get("market_regime", {})
            actual_t = inst_data.get("actual_sample_size", 0)

            # ------------------------------------------------------------------
            # 3. Market Regime Tagging & Operational Health KPI Banner (Top of Tab)
            # ------------------------------------------------------------------
            op_data = {}
            if OPERATIONAL_METRICS_JSON.exists():
                try:
                    with open(OPERATIONAL_METRICS_JSON, "r", encoding="utf-8") as f_op:
                        op_data = json.load(f_op)
                except Exception as e:
                    logger.debug(f"Could not load {OPERATIONAL_METRICS_JSON}: {e}")

            llm_fallback_rate = float(op_data.get("llm_fallback_rate_pct", 0.0))
            llm_fallbacks = int(op_data.get("llm_fallbacks", 0))
            llm_attempts = int(op_data.get("llm_calls_attempted", 0))
            freshness_blocks = int(op_data.get("freshness_blocks", 0))
            freshness_checks = int(op_data.get("freshness_checks_total", 0))
            freshness_rate = float(op_data.get("freshness_block_rate_pct", 0.0))
            nlp_total = int(op_data.get("nlp_evaluations_total", 0))
            
            # Refined health status logic for early-stage walk-forward
            raw_status = str(op_data.get("status", "INITIALIZING"))
            if llm_attempts < 10 or freshness_checks < 10:
                bot_status = "INITIALIZING (Pieni otos — odottaa syklejä)"
                status_icon = "🟡"
            elif "HEALTHY" in raw_status.upper():
                bot_status = "HEALTHY (Nimellinen toiminta)"
                status_icon = "🟢"
            else:
                bot_status = raw_status
                status_icon = "🟡"
            last_op_update = to_helsinki_time(op_data.get("last_updated", ""))

            st.markdown("#### 🏥 Markkinaregiimi & Botin Operatiivinen Terveys")
            c_op0, c_op1, c_op2, c_op3, c_op4 = st.columns(5)
            bm_sym = regime.get("benchmark", "IWC")
            bm_name = regime.get("benchmark_name", "iShares Micro-Cap ETF (IWC)")
            u_vol = regime.get("universe_median_vol_pct")
            vol_delta_str = f"20d Vol: {regime.get('volatility_20d_pct', 22.5):.1f}% | SMA50: {regime.get('dist_sma50_pct', 0.0):+.1f}%"
            if u_vol is not None:
                vol_delta_str += f" | Univ: {u_vol:.1f}%"

            with c_op0:
                st.metric(
                    f"Markkinaregiimi ({bm_sym})",
                    regime.get("badge", "🟡 NEUTRAL"),
                    vol_delta_str,
                    help=f"{regime.get('description', '')} (Lähde: {bm_name} mikroyhtiöbenchmark)",
                )
            with c_op1:
                st.metric(
                    "LLM Fallback Rate",
                    f"{llm_fallback_rate:.1f}%",
                    f"{llm_fallbacks} / {max(llm_attempts, 1)} varalla",
                    delta_color="inverse",
                    help="Kuinka usein NLP/LLM-tarkistus on aikakatkaistu tai kaatunut, ja siirrytty automaattisesti sääntöpohjaiseen (rule-based) analyysiin.",
                )
            with c_op2:
                st.metric(
                    "Data Freshness Blocks",
                    f"{freshness_blocks} kpl",
                    f"{freshness_rate:.1f}% ostoista estetty",
                    delta_color="inverse",
                    help="Kuinka monta kertaa is_fresh -suoja on estänyt ostotoimeksiannon vanhentuneen (>120 pv) tilinpäätösdatan tai yfinance-viiveen vuoksi.",
                )
            with c_op3:
                st.metric(
                    "NLP-arviointeja",
                    f"{nlp_total} kpl",
                    "Uutis- & tiedotetutka",
                    help="Yhteensä läpikäydyt pörssitiedotteet ja uutisotsikot rinnakkaisissa walk-forward -salkuissa.",
                )
            with c_op4:
                st.metric(
                    "Järjestelmän tila",
                    f"{status_icon} {bot_status.split()[0]}",
                    bot_status,
                    help="Daemonin toimintatila. Alkuvaiheessa (<10 arviointia) tila on INITIALIZING, jotta vältetään väärä turvallisuuden tunne ennen aitoja stressitapahtumia.",
                )

            st.divider()

            # ------------------------------------------------------------------
            # 4. Unified Summary Table (Rows = 10 Portfolios)
            # Columns = 30d Sharpe, 30d Sortino, Median Return, Mean Return, Max DD, Top-1 PnL %, Median Hold Days
            # ------------------------------------------------------------------
            st.markdown("#### 📋 10 Salkun Institutional-Yhteenveto")
            st.caption("Rivit = 10 salkkua. Sarakkeet = 30d Sharpe, 30d Sortino, Mediaanituotto, Keskituotto, Max Drawdown, Top-1 PnL % ja Mediaanipitoaika.")

            spec_cols = [
                "Portfolio",
                "30d Sharpe",
                "30d Sortino",
                "Median Return",
                "Mean Return",
                "Max DD",
                "Top-1 PnL %",
                "Median Hold Days",
            ]
            display_df = df_summary[[c for c in spec_cols if c in df_summary.columns]].copy()

            def _highlight_selected_and_best(row):
                styles = [""] * len(row)
                pid = row["Portfolio"]
                if pid == selected_portfolio:
                    styles = ["background-color: #1e3a5f; font-weight: bold;"] * len(row)
                elif pid == dsr_report.get("best_portfolio"):
                    styles = ["background-color: #14382c;"] * len(row)
                return styles

            styled_summary = display_df.style.apply(_highlight_selected_and_best, axis=1)
            st.dataframe(styled_summary, width="stretch", hide_index=True)
            st.caption(
                f"🔵 **Sininen korostus** = sivupalkista valittu salkku (`{selected_portfolio}`) &nbsp;|&nbsp; "
                f"🟢 **Vihreä korostus** = korkeimman Sharpen salkku (`{dsr_report.get('best_portfolio', 'P1_Base')}`)."
            )

            st.divider()

            # ------------------------------------------------------------------
            # 5. Multiple Testing Correction with DATA SUFFICIENCY GUARD
            # ------------------------------------------------------------------
            st.markdown("#### 🔬 Monitestauskorjaus & Tilastollinen Merkitsevyys (DSR)")
            st.caption(
                "Kun testataan 10 rinnakkaista strategiaa, paras salkku voi näyttää hyvältä pelkän satunnaiskohinan (selection bias) vuoksi. "
                "Deflated Sharpe Ratio (DSR, Bailey & López de Prado) ja Bonferroni-korjaus testaavat, ylittääkö tuotto satunnaisuuden kynnyksen."
            )

            has_suff_data = dsr_report.get("has_sufficient_data", False)
            best_p = dsr_report.get("best_portfolio", "P1_Base")
            best_sr = dsr_report.get("best_sharpe", 0.0)
            e_max = dsr_report.get("expected_max_sharpe", 1.57)

            if not has_suff_data:
                # DATA SUFFICIENCY GUARD: Block misleading large Null E[max] estimates on tiny samples
                req_obs = dsr_report.get("min_required_observations", 20)
                days_needed = dsr_report.get("days_needed", 20)
                st.warning(
                    f"⏳ **Datan riittävyysportti aktiivinen (Otoskoko: {actual_t} / {req_obs} havaintopäivää)**\n\n"
                    f"Tilastollisen monitestauskorjauksen (Deflated Sharpe Ratio / Bonferroni) luotettava arviointi edellyttää "
                    f"vähintään **{req_obs} tuottohavaintoa** per salkku. "
                    f"Pienellä otoskoolla ($T < 20$) Sharpe-estimaattorin varianssi kasvaa räjähdysmäisesti "
                    f"($\\sigma_0 = \\sqrt{{252/T}}$), jolloin toistetun otannan odotettu maksimi ($Null\\ E[\\max]$) antaisi "
                    f"matemaattisesti vääristyneitä ja harhaanjohtavia suurlukuja.\n\n"
                    f"➡️ **Tarvitaan vielä {days_needed} päivää lisää salkkuhistoriaa**, ennen kuin DSR-merkitsevyysanalyysi aktivoituu. "
                    f"*(Standardi 1 vuoden satunnaishuippu 10 strategialle normaalijakaumassa: $Null\\ E[\\max] \\approx {e_max:.2f}$)*."
                )
            else:
                dsr_pct = dsr_report.get("dsr", 0.0) * 100.0
                bonf_p = dsr_report.get("bonferroni_p_value", 1.0)
                is_sig = dsr_report.get("is_significant", False)

                m_col1, m_col2, m_col3, m_col4 = st.columns(4)
                with m_col1:
                    st.metric("Paras Sharpe (Huippu)", f"{best_sr:.2f}", f"Salkku: {best_p}")
                with m_col2:
                    st.metric("Odotettu satunnaishuippu (Null E[max])", f"{e_max:.2f}", f"N=10 kokeen satunnaisuus (T={actual_t})")
                with m_col3:
                    st.metric("Deflated Sharpe (DSR)", f"{dsr_pct:.1f}%", "Luotettavuus (kynnys: 95%)")
                with m_col4:
                    st.metric("Bonferroni p-arvo", f"{bonf_p:.4f}", "Kynnys: α < 0.005")

                if is_sig:
                    st.success(
                        f"✅ **Tilastollisesti Merkitsevä Tulos ({dsr_report.get('verdict')})**: "
                        f"Salkun `{best_p}` Sharpe-luku ({best_sr:.2f}) ylittää 10 strategian satunnaiskorjauksen (DSR: {dsr_pct:.1f}% > 95%). "
                        "Havaittu ylituotto ei selity pelkällä sattumalla."
                    )
                else:
                    st.info(
                        f"ℹ️ **Satunnaisvaihtelun Alueella ({dsr_report.get('verdict')})**: "
                        f"Salkun `{best_p}` Sharpe ({best_sr:.2f}) on lähellä 10 riippumattoman kokeen odotettua satunnaishuippua ({e_max:.2f}). "
                        "Datahistoriaa tarvitaan useampia viikkoja tilastollisen merkitsevyyden vahvistamiseksi."
                    )

            st.divider()

            # ------------------------------------------------------------------
            # 6. 10-Portfolio Return Correlation Matrix Heatmap (Plotly)
            # ------------------------------------------------------------------
            st.markdown("#### 🌐 10 Salkun Tuottokorrelaatiomatriisi & Hajautusanalyysi")
            st.caption(
                "Mittaa strategioiden välistä riippuvuutta. Matala tai negatiivinen korrelaatio "
                "osoittaa todellista hajautushyötyä (aidosti toisistaan poikkeavat alpha-lähteet). "
                "Viikkoaggregointi suodattaa päivänsisäisen kohinan."
            )

            if actual_t < 5:
                st.info(
                    f"⏳ **Korrelaatiomatriisin laskentaan tarvitaan vähintään 5 yhteistä päätöspäivää** "
                    f"(nykyinen otoskoko: {actual_t} pv). Salkut ovat tällä hetkellä alkutilassa."
                )

            if not corr_matrix.empty and len(corr_matrix) > 1:
                fig_corr = px.imshow(
                    corr_matrix,
                    text_auto=".2f",
                    aspect="auto",
                    color_continuous_scale="RdBu_r",
                    zmin=-1.0,
                    zmax=1.0,
                    labels=dict(x="Salkku", y="Salkku", color="Korrelaatio"),
                )
                fig_corr.update_layout(
                    plot_bgcolor="#0f172a",
                    paper_bgcolor="#0f172a",
                    height=520,
                    font=dict(color="#f1f5f9", size=11),
                    margin=dict(l=40, r=40, t=40, b=40),
                    xaxis=dict(tickangle=-45),
                )
                st.plotly_chart(fig_corr, width="stretch")
            else:
                st.info("Korrelaatiomatriisin laskentaan tarvitaan vähintään kaksi peräkkäistä snapshotia salkuista.")

            st.divider()

            # ------------------------------------------------------------------
            # 7. Attribution & Trade Skewness Deep-Dive
            # ------------------------------------------------------------------
            with st.expander("🔍 Tuottoattribuutio & Vinousanalyysi (Mediaani vs Keskiarvo & Keskittyminen)", expanded=False):
                st.markdown(
                    "**Miksi Mediaani vs Keskiarvo on tärkeä?** Jos keskiarvotuotto on merkittävästi korkeampi kuin mediaani, "
                    "salkun tuotot nojaavat harvoihin äärimmäisiin 'lottovoittoihin' (oikealle vino jakauma). "
                    "Jos mediaani on korkeampi, strategian tyypillinen kauppa on tasalaatuisempi."
                )
                c_att1, c_att2 = st.columns(2)
                with c_att1:
                    st.markdown("##### 🎯 Posiokeskittyminen (Top 1 & Top 2 Kaupat)")
                    df_conc = df_summary[["Portfolio", "Top-1 PnL %", "_trades_count"]].copy()
                    df_conc.columns = ["Salkku", "Top-1 Kauppa % PnL", "Suljetut Kaupat"]
                    st.dataframe(df_conc, width="stretch", hide_index=True)
                with c_att2:
                    st.markdown("##### ⏱️ Pitoajat & PnL-jakauma")
                    df_hold = df_summary[["Portfolio", "Median Return", "Mean Return", "Median Hold Days"]].copy()
                    df_hold.columns = ["Salkku", "Mediaanituotto", "Keskituotto", "Mediaanipitoaika"]
                    st.dataframe(df_hold, width="stretch", hide_index=True)

        with tab2:
            st.markdown("### ⚡ Avaa Uusi Virtuaalipositio (Paper Trade)")
            st.caption("Laskee automaattisesti 5 % allokaation (500 € / 10 000 €) ja asettaa -20 % alkustopin.")
            
            p_col1, p_col2 = st.columns(2)
            with p_col1:
                new_ticker = st.text_input("Osakkeen Ticker:", value="VINCIT.HE", placeholder="esim. VINCIT.HE, EVO.ST, AAPL")
                new_market = st.selectbox("Markkina:", ["FI", "SE", "US", "NO", "DK"], index=0)
            with p_col2:
                custom_alloc = st.number_input("Allokoitava pääoma (EUR/USD):", min_value=100.0, max_value=5000.0, value=500.0, step=100.0)
                execute_btn = st.button("🛒 Avaa Paperipositio", type="primary", width="stretch")

            if execute_btn and new_ticker:
                from screener.portfolio_manager import open_position
                with st.spinner(f"Haetaan kurssidata ja avataan positio osakkeelle {new_ticker}..."):
                    res = open_position(ticker=new_ticker, market=new_market, allocated_capital=custom_alloc)
                if res:
                    st.success(f"✅ Positio avattu! {res['ticker']} | {res['shares']} kpl @ {res['buy_price']} {res['currency']} | Alkustop (-20%): {res['trailing_stop_price']}")
                    time.sleep(1.5)
                    st.rerun()
                else:
                    st.warning(f"Positiota ei voitu avata (ehkä jo avoinna tai kurssidatan nouto epäonnistui).")

        with tab3:
            trade_cols = [
                "Ticker", "Buy Date", "Sell Date", "Buy Price", "Sell Price",
                "Shares", "Capital Invested", "Gross Sale Value", "Transaction Fee",
                "Net Return", "Net PnL", "Exit Reason"
            ]

            active_trade_history_file = port_history_csv if port_history_csv.exists() else TRADE_HISTORY_CSV

            # Auto-repair mismatched header in trade history (e.g. 9 headers vs 12 data columns)
            if active_trade_history_file.exists():
                try:
                    with open(active_trade_history_file, "r", encoding="utf-8") as f_th:
                        th_lines = f_th.readlines()
                    if th_lines:
                        h_parts = [p.strip() for p in th_lines[0].strip().split(",")]
                        if len(h_parts) != len(trade_cols) and len(th_lines) > 1:
                            d_parts = [p.strip() for p in th_lines[1].strip().split(",")]
                            if len(d_parts) == len(trade_cols):
                                th_lines[0] = ",".join(trade_cols) + "\n"
                                with open(active_trade_history_file, "w", encoding="utf-8") as f_out:
                                    f_out.writelines(th_lines)
                except Exception:
                    pass

            df_trades = load_csv_safely(active_trade_history_file)
            if df_trades.empty:
                st.info(f"Ei toteutuneita kauppoja salkussa `{selected_portfolio}`.")
            else:
                for col in ["timestamp", "entrydate", "exitdate", "date", "buy date", "sell date"]:
                    if col in df_trades.columns:
                        df_trades[col] = df_trades[col].apply(to_helsinki_time)

                # Check realized PnL column
                pnl_col = None
                for candidate in ["net pnl", "net_pnl", "realized_pnl", "netpnleur", "net_pnl_eur", "pnl_absolute"]:
                    if candidate in df_trades.columns:
                        pnl_col = candidate
                        break

                if pnl_col:
                    total_pnl = pd.to_numeric(df_trades[pnl_col], errors="coerce").fillna(0.0).sum()
                    st.metric("Kokonais PnL (Realisoitunut)", f"{total_pnl:+,.2f} EUR")

                st.dataframe(df_trades, width="stretch", hide_index=True)

        with tab4:
            df_alerts = load_csv_safely(SCREENER_ALERTS_CSV)
            if df_alerts.empty:
                st.info("Ei hälytyksiä tiedostossa `data/screener_alerts.csv`.")
            else:
                for col in ["timestamp", "date", "created_at"]:
                    if col in df_alerts.columns:
                        df_alerts[col] = df_alerts[col].apply(to_helsinki_time)
                st.dataframe(df_alerts, width="stretch", hide_index=True)

        # ------------------------------------------------------------------
        # TAB 5: Multi-Portfolio Equity Curve Comparison
        # ------------------------------------------------------------------
        with tab5:
            st.markdown("### 📊 Kaikkien Salkkujen Kehitysvertailu")
            st.caption(
                "Vertaile kaikkien 10 rinnakkaisen walk-forward-testisalkun pääoman kehitystä. "
                "Jokainen käyrä edustaa yhtä salkun kokonaispääoman (käteinen + osakkeet) kehitystä ajan kuluessa."
            )

            # Toggle: absolute vs normalised (base 100)
            norm_toggle = st.toggle(
                "Normalisoitu vertailu (lähtötaso = 100)",
                value=False,
                help="Normalisoitu näkymä poistaa alkupääoman eron ja näyttää suhteellisen tuoton.",
            )

            # Load all portfolio history files
            port_traces = {}
            summary_rows = []

            all_port_ids = portfolio_options if portfolio_options else [f"P{i}" for i in range(1, 11)]

            for pid in all_port_ids:
                eq_file = PORTFOLIOS_DIR / f"portfolio_{pid}_history.json"
                if not eq_file.exists():
                    continue
                try:
                    with open(eq_file, "r", encoding="utf-8") as _ef:
                        eq_data = json.load(_ef)
                    if not eq_data or not isinstance(eq_data, list):
                        continue
                    df_eq = pd.DataFrame(eq_data)
                    if "timestamp" not in df_eq.columns or "total_equity" not in df_eq.columns:
                        continue
                    df_eq["timestamp"] = pd.to_datetime(df_eq["timestamp"], errors="coerce")
                    df_eq["total_equity"] = pd.to_numeric(df_eq["total_equity"], errors="coerce")
                    df_eq = df_eq.dropna(subset=["timestamp", "total_equity"]).sort_values("timestamp")
                    if df_eq.empty:
                        continue
                    port_traces[pid] = df_eq

                    latest = df_eq.iloc[-1]
                    start_eq = df_eq.iloc[0]["total_equity"]
                    latest_eq = float(latest["total_equity"])
                    ret_eur = latest_eq - start_eq
                    ret_pct = (ret_eur / start_eq * 100) if start_eq else 0.0
                    summary_rows.append({
                        "Salkku": pid,
                        "Viimeisin pääoma (€)": round(latest_eq, 2),
                        "Tuotto (€)": round(ret_eur, 2),
                        "Tuotto (%)": round(ret_pct, 2),
                        "Snapshotit": len(df_eq),
                        "Viimeisin päivitys": latest["timestamp"].strftime("%d.%m.%Y %H:%M"),
                    })
                except Exception:
                    continue

            if not port_traces:
                st.info(
                    "Ei equity-historiadataa saatavilla. "
                    "Daemon tallentaa snapshotteja ajon lopussa tiedostoihin "
                    "`data/portfolios/portfolio_<ID>_history.json`."
                )
            else:
                # Build Plotly figure
                PALETTE = [
                    "#38bdf8", "#10b981", "#f59e0b", "#f43f5e", "#a78bfa",
                    "#fb923c", "#34d399", "#e879f9", "#facc15", "#60a5fa",
                ]
                fig_comp = go.Figure()
                for i, (pid, df_eq) in enumerate(port_traces.items()):
                    y_vals = df_eq["total_equity"].tolist()
                    if norm_toggle and y_vals:
                        base = y_vals[0]
                        y_vals = [v / base * 100 for v in y_vals] if base else y_vals
                    color = PALETTE[i % len(PALETTE)]
                    is_active = (pid == selected_portfolio)
                    fig_comp.add_trace(go.Scatter(
                        x=df_eq["timestamp"].tolist(),
                        y=y_vals,
                        mode="lines+markers",
                        name=pid,
                        line=dict(
                            color=color,
                            width=3 if is_active else 1.5,
                            dash="solid" if is_active else "dot",
                        ),
                        marker=dict(size=5 if is_active else 3),
                        opacity=1.0 if is_active else 0.65,
                        hovertemplate=(
                            f"<b>{pid}</b><br>"
                            "%{x|%d.%m.%Y %H:%M}<br>"
                            + ("Indeksi: %{y:.1f}" if norm_toggle else "Pääoma: %{y:,.2f} €")
                            + "<extra></extra>"
                        ),
                    ))

                y_axis_title = "Indeksi (lähtötaso=100)" if norm_toggle else "Kokonaispääoma (€)"
                if norm_toggle:
                    fig_comp.add_hline(
                        y=100, line_dash="dash", line_color="#64748b", line_width=1,
                        annotation_text="Lähtötaso 100", annotation_position="bottom right",
                        annotation_font_color="#94a3b8",
                    )

                fig_comp.update_layout(
                    title="📊 Kaikkien Salkkujen Pääomakehitys",
                    template="plotly_dark",
                    paper_bgcolor="#1e293b",
                    plot_bgcolor="#0f172a",
                    height=480,
                    margin=dict(l=60, r=40, t=60, b=50),
                    legend=dict(
                        orientation="v",
                        x=1.01, y=1,
                        bgcolor="rgba(0,0,0,0)",
                        font=dict(size=11),
                    ),
                    xaxis=dict(title="Aika", gridcolor="#1e293b"),
                    yaxis=dict(title=y_axis_title, gridcolor="#334155"),
                    hovermode="x unified",
                )
                st.plotly_chart(fig_comp, use_container_width=True)
                st.caption(
                    f"🔵 **Paksu viiva** = aktiivinen salkku ({selected_portfolio}). "
                    "Pisteet piirtyvät daemon-ajojen yhteydessä tallennetuista snapshotista."
                )

                # Summary table
                if summary_rows:
                    st.markdown("#### 📋 Yhteenveto — Kaikkien Salkkujen Tila")
                    df_summary = pd.DataFrame(summary_rows)
                    # Highlight active portfolio row
                    def _highlight_active(row):
                        if row["Salkku"] == selected_portfolio:
                            return ["background-color: #1e3a5f"] * len(row)
                        return [""] * len(row)

                    styled = df_summary.style.apply(_highlight_active, axis=1).format({
                        "Viimeisin pääoma (€)": "{:,.2f}",
                        "Tuotto (€)": "{:+,.2f}",
                        "Tuotto (%)": "{:+.2f}%",
                    })
                    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ------------------------------------------------------------------
    # VIEW 4: LIVE TICKER SCANNER
    # ------------------------------------------------------------------
    elif active_menu == "🧪 Live Ticker Scanner":
        st.markdown('<div class="main-header">🧪 Interactive Live Ticker Scanner</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Run live dual-step analysis (NLP + Google News RSS verification) on any company on demand.</div>', unsafe_allow_html=True)

        col1, col2 = st.columns([2, 3])
        with col1:
            ticker_input = st.text_input(
                "Enter Ticker:",
                value="",
                placeholder="e.g. ADMCM.HE, QTCOM.HE, UAVS, CHPT",
                help="e.g. UAVS, CHPT, QTCOM.HE, MYPS, ESCA, ADMCM.HE",
            )
            company_input = st.text_input(
                "Company Name (optional):",
                value="",
                placeholder="e.g. Admicom, Qt Group, AgEagle Aerial Systems",
            )
            sample_text = st.text_area(
                "Paste Earnings / Filing Text (or leave blank for web sanity check only):",
                height=200,
                placeholder="Paste financial summary, 10-Q excerpt, or press release here...",
            )
            run_btn = st.button("🚀 Run Dual-Step Analysis", type="primary", width="stretch")

        with col2:
            if run_btn:
                if not ticker_input.strip():
                    st.warning("⚠️ Syötä vähintään osaketikkeri (esim. ADMCM.HE tai UAVS).")
                else:
                    from screener.nlp_analyzer import analyze_core_fundamentals
                    from screener.web_verifier import WebSearchVerifier

                    with st.spinner(f"Querying live news & analyzing {ticker_input.strip().upper()}..."):
                        verifier = WebSearchVerifier()
                        web_res = verifier.verify(ticker_input.strip().upper(), company_input.strip())

                        doc_res = {}
                        if sample_text.strip():
                            doc_res = analyze_core_fundamentals(sample_text)

                        st.session_state["scanner_last_result"] = {
                            "ticker": ticker_input.strip().upper(),
                            "company_name": company_input.strip() or ticker_input.strip().upper(),
                            "web_res": web_res,
                            "doc_res": doc_res,
                        }

            scan_res = st.session_state.get("scanner_last_result")
            if scan_res:
                t_symbol = scan_res["ticker"]
                web_res = scan_res["web_res"]
                doc_res = scan_res["doc_res"]
                comp_name = scan_res["company_name"]

                st.success(f"Analysis complete for **{t_symbol}**!")

                # Results Display
                st.markdown("### 📋 Dual-Step Verdict")
                v_col1, v_col2 = st.columns(2)
                with v_col1:
                    st.metric("Web Sanity Check", "✅ PASSED" if web_res.get("passed_web_check") else "❌ FAILED (Red Flags)")
                with v_col2:
                    st.metric("Turnaround Catalyst", "🚀 DETECTED" if web_res.get("turnaround_catalyst_detected") else "None Detected")

                if web_res.get("red_flags_found"):
                    st.error(f"**Red Flags Detected:** {', '.join(web_res['red_flags_found'])}")
                if web_res.get("positive_catalysts_found"):
                    st.info(f"**Positive Catalysts:** {', '.join(web_res['positive_catalysts_found'])}")

                # Save to Turnaround Watchlist Button
                st.markdown("---")
                is_in_watch = is_ticker_in_watchlist(WATCHLIST_CSV, t_symbol)
                if is_in_watch:
                    st.info(f"📌 **{t_symbol}** on jo mukana Turnaround Watchlistissä.")
                else:
                    if st.button("➕ Tallenna Turnaround Watchlistiin", type="secondary", key="save_to_watchlist_btn"):
                        catalysts = web_res.get("positive_catalysts_found", [])
                        snippets = web_res.get("snippets", [])
                        news_link = snippets[0] if snippets else ""
                        entry = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "company_name": comp_name,
                            "ticker": t_symbol,
                            "strategy_type": "TURNAROUND",
                            "title": f"Manual Scan: {comp_name}",
                            "link": news_link,
                            "doc_verdict": doc_res.get("verdict", "MANUAL_SCAN") if doc_res else "MANUAL_SCAN",
                            "doc_reasoning": doc_res.get("reasoning", "Added via Live Ticker Scanner") if doc_res else "Added via Live Ticker Scanner",
                            "positive_catalysts_found": catalysts if catalysts else ["Turnaround catalyst detected"],
                            "web_reasoning": web_res.get("reason", "Live search confirmed turnaround catalysts."),
                        }
                        if append_to_turnaround_watchlist(WATCHLIST_CSV, entry):
                            st.success(f"✅ **{t_symbol}** lisätty onnistuneesti Turnaround Watchlistiin! Näkyy nyt '👀 Turnaround Watchlist' -välilehdellä.")
                            st.rerun()

                st.markdown("#### 📰 Live News & Snippets Reviewed:")
                snippets = web_res.get("snippets", [])
                if snippets:
                    for idx, snip in enumerate(snippets[:6], 1):
                        st.markdown(f"- **[{idx}]** {snip}")
                else:
                    st.caption("No recent news snippets found.")

                if doc_res:
                    st.markdown("#### 🧠 Document Analysis Rationale:")
                    st.write(doc_res.get("reasoning", "N/A"))


# ----------------------------------------------------------------------
# STATIC FRAGMENT WRAPPERS (Module-level for zero dimming / gray-out)
# selected_portfolio is passed as a parameter so that when the user switches
# portfolios in the sidebar, the argument value changes and Streamlit is
# forced to re-execute the fragment body (fragments only re-run on argument
# change, timer tick, or internal widget interaction).
# ----------------------------------------------------------------------
@st.fragment(run_every=10)
def _fragment_view_10(active_menu: str, selected_portfolio: str):
    render_dashboard_views(active_menu)

@st.fragment(run_every=30)
def _fragment_view_30(active_menu: str, selected_portfolio: str):
    render_dashboard_views(active_menu)

@st.fragment(run_every=60)
def _fragment_view_60(active_menu: str, selected_portfolio: str):
    render_dashboard_views(active_menu)

@st.fragment(run_every=300)
def _fragment_view_300(active_menu: str, selected_portfolio: str):
    render_dashboard_views(active_menu)

@st.fragment
def _fragment_view_manual(active_menu: str, selected_portfolio: str):
    render_dashboard_views(active_menu)


# Execute fragment renderer
_active_portfolio = st.session_state.get("selected_portfolio", "P1_Base")
if auto_refresh:
    interval_val = int(refresh_interval) if refresh_interval else 30
    if interval_val <= 10:
        _fragment_view_10(menu, _active_portfolio)
    elif interval_val <= 30:
        _fragment_view_30(menu, _active_portfolio)
    elif interval_val <= 60:
        _fragment_view_60(menu, _active_portfolio)
    else:
        _fragment_view_300(menu, _active_portfolio)
else:
    _fragment_view_manual(menu, _active_portfolio)

