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
import zoneinfo
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px

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

# Page Configuration
st.set_page_config(
    page_title="tradeBotTiuku — Quantitative Screener & Turnaround Hub",
    page_icon="🐱",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Timezone Helper (Helsinki Time with 1-second accuracy)
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
        
        try:
            helsinki_tz = zoneinfo.ZoneInfo("Europe/Helsinki")
            dt_local = dt.astimezone(helsinki_tz)
        except Exception:
            dt_local = dt.astimezone()

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


@st.cache_data(ttl=60)
def fetch_positions_live_data(tickers_tuple: tuple) -> dict:
    """Fetches real-time price, day change, and currency for a tuple of tickers with fallback logic."""
    from screener.price_fetcher import get_realtime_data
    live_map = {}
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
            "📊 Live Screener Results",
            "👀 Turnaround Watchlist",
            "💰 Paper Trading Portfolio",
            "🧪 Live Ticker Scanner",
        ],
        index=0,
    )

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

    # Token Usage & Cost Counter
    try:
        from core.token_tracker import get_token_stats, tracker
        token_stats = get_token_stats()
        total_tokens = token_stats.get("total_tokens", 0)
        prompt_tokens = token_stats.get("total_prompt_tokens", 0)
        completion_tokens = token_stats.get("total_completion_tokens", 0)
        total_cost = token_stats.get("total_cost_usd", 0.0)
        total_reqs = token_stats.get("total_requests", 0)
        
        with st.expander("🪙 Token-laskuri & Kustannus", expanded=False):
            t_col1, t_col2 = st.columns(2)
            with t_col1:
                st.metric("Kokonaiskulutus", f"${total_cost:.4f}")
            with t_col2:
                st.metric("Pyyntöjä", f"{total_reqs} kpl")
                
            st.markdown(f"**Tokenit yhteensä:** `{total_tokens:,}`")
            st.caption(f"- Syöte (Prompt): `{prompt_tokens:,}`\n- Tuotos (Output): `{completion_tokens:,}`")
            
            # Estimate capacity from $10 OpenRouter balance
            rem_budget = max(0.0, 10.0 - total_cost)
            avg_cost = (total_cost / total_reqs) if total_reqs > 0 else 0.00055
            rem_reports = int(rem_budget / avg_cost) if avg_cost > 0 else 18000
            st.info(f"💡 **10 $ saldolla jäljellä:** n. **${rem_budget:.2f}** (~{rem_reports:,} analyysiä)")
            
            if st.button("🗑️ Nollaa laskuri", width="stretch"):
                tracker.reset_stats()
                st.success("Laskuri nollattu!")
                time.sleep(0.5)
                st.rerun()
    except Exception as e:
        logger.debug(f"Failed to render token tracker in dashboard: {e}")

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

                with st.expander(f"📌 **{ticker}** ({comp}) — *{catalysts_str}* | 🕒 {ts}", expanded=True):
                    c_left, c_right = st.columns(2)
                    with c_left:
                        st.markdown("#### ⚠️ Step 1: Historical Financial Lag (10-Q / PR)")
                        st.caption(f"Initial Verdict: `{row.get('doc_verdict', 'HOLD / REJECT')}`")
                        st.warning(row.get("doc_reasoning", "Historical losses or low margins reported in past quarter."))
                    with c_right:
                        st.markdown("#### 🚀 Step 2: Real-Time Positive Catalysts (Web / News)")
                        st.caption(f"Catalysts: `{catalysts_str}`")
                        st.success(row.get("web_reasoning", "Live search confirms positive restructuring, sales beat, or contract win."))

                    if row.get("link"):
                        st.markdown(f"[🔗 Open Disclosure / News Article]({row['link']})")

    # ------------------------------------------------------------------
    # VIEW 3: PAPER TRADING PORTFOLIO & RISK CONTROLS
    # ------------------------------------------------------------------
    elif active_menu == "💰 Paper Trading Portfolio":
        st.markdown('<div class="main-header">💰 Virtual Paper Trading Portfolio & Risk Controls</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Live virtual portfolio tracking, 5% position sizing, and strict -20% Trailing Stop-Loss enforcement.</div>', unsafe_allow_html=True)

        tab1, tab2, tab3, tab4 = st.tabs([
            "📌 Avoimet Positiot & Trailing Stopit",
            "⚡ Riskinhallinta & Testiosto",
            "📜 Toteutuneet Kaupat (Trade History)",
            "🔔 Hälytysloki (Screener Alerts)",
        ])

        with tab1:
            df_pos = load_csv_safely(OPEN_POSITIONS_CSV)
            if df_pos.empty:
                st.info("Ei avoimia paperipositioita tiedostossa `data/open_positions.csv`. Voit avata uuden position '⚡ Riskinhallinta & Testiosto' -välilehdeltä tai ajaa seulonnan.")
            else:
                # Load paper account balance
                starting_capital = 10_000.0
                free_cash = 0.72
                if PAPER_ACCOUNT_JSON.exists():
                    try:
                        with open(PAPER_ACCOUNT_JSON, "r", encoding="utf-8") as f_acc:
                            acc_data = json.load(f_acc)
                            starting_capital = float(acc_data.get("starting_balance", 10_000.0))
                            free_cash = float(acc_data.get("cash_balance", 0.72))
                    except Exception:
                        pass

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
                        or "-"
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
                    if buy_price <= 0.0 and cap_invested > 0.0 and shares > 0.0:
                        buy_price = round(cap_invested / shares, 4)
                    if cap_invested <= 0.0 and buy_price > 0.0 and shares > 0.0:
                        cap_invested = round(buy_price * shares, 2)

                    strategy = r.get("strategy_type", r.get("strategy", "SATELLITE"))

                    q = live_quotes.get(t_sym, {})
                    curr_price = float(q.get("current_price") or buy_price)
                    day_chg = float(q.get("day_change_pct") or 0.0)
                    curr_curr = q.get("currency") or "USD"

                    pos_mkt_val = shares * curr_price
                    pos_pnl_abs = pos_mkt_val - cap_invested
                    pos_pnl_pct = ((curr_price - buy_price) / buy_price * 100.0) if buy_price > 0 else 0.0
                    cat_stop = buy_price * 0.50
                    stop_dist_pct = ((curr_price - cat_stop) / curr_price * 100.0) if curr_price > 0 else 0.0

                    total_invested += cap_invested
                    total_market_val += pos_mkt_val

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
                        "cat_stop": cat_stop,
                        "stop_dist_pct": stop_dist_pct,
                        "strategy": strategy,
                        "currency": curr_curr,
                    })

                df_enriched = pd.DataFrame(enriched_rows)

                # Portfolio KPI calculations
                unrealized_pnl = total_market_val - total_invested
                unrealized_pnl_pct = (unrealized_pnl / total_invested * 100.0) if total_invested > 0 else 0.0
                total_equity = free_cash + total_market_val
                portfolio_total_return = total_equity - starting_capital
                portfolio_total_return_pct = (portfolio_total_return / starting_capital * 100.0) if starting_capital > 0 else 0.0

                win_count = sum(1 for row in enriched_rows if row["pnl_pct"] > 0)
                loss_count = sum(1 for row in enriched_rows if row["pnl_pct"] < 0)

                # Render Top KPI Cards
                kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
                with kpi1:
                    st.metric(
                        "Salkun Kokonaisarvo",
                        f"${total_equity:,.2f}",
                        f"{portfolio_total_return:+,.2f} ({portfolio_total_return_pct:+.2f}%)",
                    )
                with kpi2:
                    st.metric("Sijoitettu Pääoma", f"${total_invested:,.2f}")
                with kpi3:
                    st.metric(
                        "Avoin Tuotto (PnL)",
                        f"${unrealized_pnl:+,.2f}",
                        f"{unrealized_pnl_pct:+.2f}%",
                    )
                with kpi4:
                    st.metric("Vapaa Käteinen", f"${free_cash:,.2f}")
                with kpi5:
                    st.metric(
                        "Avoimet Positiot",
                        f"{len(df_enriched)} kpl",
                        f"{win_count} 🟢 / {loss_count} 🔴",
                    )

                st.divider()

                # Action buttons row
                act_col1, act_col2, act_col3 = st.columns([2, 1, 1])
                with act_col1:
                    st.markdown("##### 📈 Reaaliaikainen Osakekehitys & Tri-Layer Stopit")
                    st.caption("Kurssit noudetaan suoraan markkinarajapinnasta. Tuottoluvut ja arvot päivittyvät reaaliajassa.")
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
                        title="📊 Salkun Osakkeiden Tuotto Ostohetkestä (%)",
                        template="plotly_dark",
                        paper_bgcolor="#1e293b",
                        plot_bgcolor="#0f172a",
                        height=320,
                        margin=dict(l=90, r=40, t=40, b=30),
                        xaxis=dict(title="Tuotto %", zeroline=True, zerolinecolor="#64748b", zerolinewidth=1.5),
                        yaxis=dict(title=""),
                    )
                    st.plotly_chart(fig_bar, use_container_width=True)

                # ------------------------------------------------------------------
                # Visual 2: Formatted Portfolio Table
                # ------------------------------------------------------------------
                st.markdown("#### 📋 Avoimet Positiot ja Tuloserittely")
                
                table_display = df_enriched.copy()
                table_display["Ostohinta"] = table_display.apply(lambda r: f"{r['buy_price']:,.2f} {r['currency']}", axis=1)
                table_display["Nykykurssi"] = table_display.apply(lambda r: f"{r['curr_price']:,.2f} {r['currency']}", axis=1)
                table_display["Päivämuutos"] = table_display["day_change_pct"].apply(lambda v: f"{v:+.2f}%")
                table_display["Hankinta-arvo"] = table_display.apply(lambda r: f"${r['cap_invested']:,.2f}", axis=1)
                table_display["Markkina-arvo"] = table_display.apply(lambda r: f"${r['market_value']:,.2f}", axis=1)
                table_display["Voitto/Tappio"] = table_display.apply(lambda r: f"${r['pnl_abs']:+,.2f}", axis=1)
                table_display["Tuotto %"] = table_display["pnl_pct"].apply(lambda v: f"{v:+.2f}%")
                table_display["Stop-loss (-50%)"] = table_display.apply(lambda r: f"{r['cat_stop']:,.2f} {r['currency']}", axis=1)
                table_display["Puskuri stoppiin"] = table_display["stop_dist_pct"].apply(lambda v: f"{v:.1f}%")

                final_cols = [
                    "ticker", "buy_date", "Ostohinta", "Nykykurssi", "Päivämuutos",
                    "shares", "Hankinta-arvo", "Markkina-arvo", "Voitto/Tappio", "Tuotto %",
                    "Stop-loss (-50%)", "Puskuri stoppiin", "strategy"
                ]
                final_renames = {
                    "ticker": "Ticker",
                    "buy_date": "Ostopäivä",
                    "shares": "Kpl",
                    "strategy": "Strategia",
                }
                st.dataframe(
                    table_display[final_cols].rename(columns=final_renames),
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

                        fig_stock.update_layout(
                            title=f"📈 {selected_ticker} ({resolved_ticker}) — Kurssikehitys ja Positiotasot",
                            template="plotly_dark",
                            paper_bgcolor="#1e293b",
                            plot_bgcolor="#0f172a",
                            height=420,
                            margin=dict(l=50, r=40, t=50, b=40),
                            xaxis=dict(title="Päivämäärä", showgrid=True, gridcolor="#334155"),
                            yaxis=dict(title=f"Kurssi ({pos_match['currency']})", showgrid=True, gridcolor="#334155"),
                            hovermode="x unified",
                        )
                        st.plotly_chart(fig_stock, use_container_width=True)

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
            df_trades = load_csv_safely(TRADE_HISTORY_CSV)
            if df_trades.empty:
                st.info("Ei toteutuneita kauppoja tiedostossa `data/trade_history.csv`.")
            else:
                for col in ["timestamp", "entrydate", "exitdate", "date"]:
                    if col in df_trades.columns:
                        df_trades[col] = df_trades[col].apply(to_helsinki_time)
                
                # Check realized PnL column
                pnl_col = "realized_pnl" if "realized_pnl" in df_trades.columns else ("netpnl_eur" if "netpnl_eur" in df_trades.columns else None)
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
    # VIEW 4: LIVE TICKER SCANNER
    # ------------------------------------------------------------------
    elif active_menu == "🧪 Live Ticker Scanner":
        st.markdown('<div class="main-header">🧪 Interactive Live Ticker Scanner</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Run live dual-step analysis (NLP + Google News RSS verification) on any company on demand.</div>', unsafe_allow_html=True)

        col1, col2 = st.columns([2, 3])
        with col1:
            ticker_input = st.text_input("Enter Ticker:", value="UAVS", help="e.g. UAVS, CHPT, QTCOM.HE, MYPS, ESCA")
            company_input = st.text_input("Company Name (optional):", value="AgEagle Aerial Systems")
            sample_text = st.text_area(
                "Paste Earnings / Filing Text (or leave blank for web sanity check only):",
                height=200,
                placeholder="Paste financial summary, 10-Q excerpt, or press release here...",
            )
            run_btn = st.button("🚀 Run Dual-Step Analysis", type="primary", width="stretch")

        with col2:
            if run_btn and ticker_input:
                from screener.nlp_analyzer import analyze_core_fundamentals
                from screener.web_verifier import WebSearchVerifier

                with st.spinner(f"Querying live news & analyzing {ticker_input}..."):
                    verifier = WebSearchVerifier()
                    web_res = verifier.verify(ticker_input, company_input)

                    doc_res = {}
                    if sample_text.strip():
                        doc_res = analyze_core_fundamentals(sample_text)

                st.success(f"Analysis complete for **{ticker_input}**!")

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


# Execute fragment renderer
if auto_refresh:
    @st.fragment(run_every=int(refresh_interval))
    def live_fragment():
        render_dashboard_views(menu)
    live_fragment()
else:
    render_dashboard_views(menu)
