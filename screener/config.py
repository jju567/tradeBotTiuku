"""
Configuration settings for Nasdaq OMX Helsinki stock screener.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import os


@dataclass
class ScreenerConfig:
    # Storage settings
    db_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "data" / "screener_state.db")
    
    # RSS Feed Endpoints for Nasdaq OMX Helsinki & First North
    rss_feeds: List[str] = field(default_factory=lambda: [
        # GlobeNewswire Helsinki exchange releases & Finland Wire
        "https://www.globenewswire.com/RssFeed/exchange/HEX/feedTitle/GlobeNewswire%20-%20Exchange%20HEX",
        "https://www.globenewswire.com/RssFeed/country/FI/feedTitle/GlobeNewswire%20-%20News%20from%20Finland",
        # Cision Wire Finland (MAR releases & company news)
        "https://news.cision.com/fi/rss/all",
        # Nasdaq CDS Public RSS Feeds (Helsinki Main Market & First North)
        "https://newsclient.omxgroup.com/cds-public/view/rss/HEX/releases.rss",
        "https://newsclient.omxgroup.com/cds-public/view/rss/FNFI/releases.rss",
        "https://newsclient.omxgroup.com/cds-public/view/rss/HEX/company-news.rss",
    ])
    
    # Request & Networking settings
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 (TiukuTradeBot/1.0; +https://github.com)"
    )
    request_timeout_seconds: int = 15
    max_concurrent_requests: int = 5
    rate_limit_delay_seconds: float = 1.0  # Politeness delay between batch requests
    max_retries: int = 3
    retry_backoff_factor: float = 1.5
    
    # PDF & Content Parsing settings
    max_pdf_size_bytes: int = 15 * 1024 * 1024  # 15 MB limit per report
    max_text_characters: int = 100_000          # Cap text payload to avoid excessive memory / tokens
    extract_tables: bool = True
    
    # Quantitative Risk Filter Defaults
    min_cash_runway_months: float = 18.0
    max_bid_ask_spread_pct: float = 4.0
    
    # Portfolio Splitting & Strategy Allocation Defaults
    core_capital_pct: float = 0.75       # 75% for long-term fundamental Core Tenbagger Hunting
    satellite_capital_pct: float = 0.25  # 25% for daily catalyst Satellite Trading
    
    # Core Fundamental Screening Defaults (Rule of 40 & Quality Checklist)
    core_min_gross_margin_pct: float = 40.0   # Gross margin > 40%
    core_min_rule_of_40: float = 40.0         # Revenue Growth % + Profit Margin % >= 40%

    # Nordnet Small User Commission & Friction Settings
    # Nordnet Taso 3 (Pienkäyttäjä / 1-10 kauppaa/kk): Kotimaa min 7.00 EUR (0.15%) tai Taso 4 (min 9.00 EUR / 0.20%)
    nordnet_min_commission_eur: float = 7.00
    nordnet_commission_percent: float = 0.0015
    nordnet_min_trade_eur: float = 500.0
    max_total_friction_pct: float = 5.5  # Max combined spread + round-trip commission %
    
    # Dynamic Asymmetric Polling Schedule
    # Peak rush (08:30 - 10:00 on weekdays): Aggressive 40s polling (80% of critical earnings/MAR releases)
    # Regular market hours (10:00 - 18:30): 5 minutes (300s)
    # Off-market / nights / weekends: 15 minutes (900s)
    peak_interval_seconds: int = 40
    regular_interval_seconds: int = 300
    offmarket_interval_seconds: int = 900
    peak_start_time: str = "08:30"
    peak_end_time: str = "10:00"
    market_open_time: str = "08:00"
    market_close_time: str = "18:30"
    
    # State / History limits
    max_history_days: int = 90

    @classmethod
    def from_env(cls) -> "ScreenerConfig":
        """Load optional configuration overrides from environment variables."""
        db_env = os.getenv("SCREENER_DB_PATH")
        db_path = Path(db_env) if db_env else None
        
        cfg = cls()
        if db_path:
            cfg.db_path = db_path
        
        min_runway = os.getenv("SCREENER_MIN_CASH_RUNWAY_MONTHS")
        if min_runway:
            cfg.min_cash_runway_months = float(min_runway)
            
        max_spread = os.getenv("SCREENER_MAX_BID_ASK_SPREAD_PCT")
        if max_spread:
            cfg.max_bid_ask_spread_pct = float(max_spread)

        core_cap = os.getenv("CORE_CAPITAL_PCT") or os.getenv("SCREENER_CORE_CAPITAL_PCT")
        if core_cap:
            cfg.core_capital_pct = float(core_cap)

        sat_cap = os.getenv("SATELLITE_CAPITAL_PCT") or os.getenv("SCREENER_SATELLITE_CAPITAL_PCT")
        if sat_cap:
            cfg.satellite_capital_pct = float(sat_cap)

        min_gross_m = os.getenv("CORE_MIN_GROSS_MARGIN_PCT")
        if min_gross_m:
            cfg.core_min_gross_margin_pct = float(min_gross_m)

        min_r40 = os.getenv("CORE_MIN_RULE_OF_40")
        if min_r40:
            cfg.core_min_rule_of_40 = float(min_r40)

        min_comm = os.getenv("COMMISSION_MIN_EUR") or os.getenv("NORDNET_MIN_COMMISSION_EUR")
        if min_comm:
            cfg.nordnet_min_commission_eur = float(min_comm)

        comm_pct = os.getenv("COMMISSION_PERCENT") or os.getenv("NORDNET_COMMISSION_PERCENT")
        if comm_pct:
            cfg.nordnet_commission_percent = float(comm_pct)

        max_friction = os.getenv("MAX_TOTAL_FRICTION_PCT")
        if max_friction:
            cfg.max_total_friction_pct = float(max_friction)
            
        return cfg


def get_dynamic_interval(
    now: Optional[datetime] = None,
    config: Optional[ScreenerConfig] = None
) -> Tuple[int, str]:
    """
    Calculates dynamic asymmetric scraping interval based on Helsinki market schedule:
    - Morning Peak Rush (08:30 - 10:00, Mon-Fri): 40 seconds (80% of critical earnings/MAR releases)
    - Regular Market Hours (08:00 - 18:30, Mon-Fri): 300 seconds (5 minutes)
    - Off-Market / Night / Weekends: 900 seconds (15 minutes)

    Returns:
        (interval_seconds, mode_description)
    """
    cfg = config or ScreenerConfig()
    dt = now or datetime.now()

    # Check weekend (Mon=0, Sun=6)
    if dt.weekday() >= 5:
        return cfg.offmarket_interval_seconds, "Weekend Off-Market (15 min)"

    # Parse hour:minute bounds
    def _parse_hm(time_str: str) -> Tuple[int, int]:
        parts = time_str.split(":")
        return int(parts[0]), int(parts[1])

    current_minutes = dt.hour * 60 + dt.minute

    peak_start_h, peak_start_m = _parse_hm(cfg.peak_start_time)
    peak_end_h, peak_end_m = _parse_hm(cfg.peak_end_time)
    market_open_h, market_open_m = _parse_hm(cfg.market_open_time)
    market_close_h, market_close_m = _parse_hm(cfg.market_close_time)

    peak_start_min = peak_start_h * 60 + peak_start_m
    peak_end_min = peak_end_h * 60 + peak_end_m
    mkt_open_min = market_open_h * 60 + market_open_m
    mkt_close_min = market_close_h * 60 + market_close_m

    if peak_start_min <= current_minutes < peak_end_min:
        return cfg.peak_interval_seconds, f"Morning Peak Rush ({cfg.peak_start_time}-{cfg.peak_end_time}, {cfg.peak_interval_seconds}s)"
    elif mkt_open_min <= current_minutes < mkt_close_min:
        return cfg.regular_interval_seconds, f"Regular Market Hours ({cfg.regular_interval_seconds//60} min)"
    else:
        return cfg.offmarket_interval_seconds, f"Off-Market / Night ({cfg.offmarket_interval_seconds//60} min)"

