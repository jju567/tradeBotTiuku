import os
from pathlib import Path


def load_env_file(filepath: str = ".env") -> dict:
    """Reads key-value pairs from a .env file."""
    configs = {}
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        configs[key.strip()] = val.strip().strip('"').strip("'")
        except Exception as e:
            print(f"Warning: Could not read .env file at {filepath}: {e}")
    return configs


# Load environment variables
env = load_env_file()

# Base & Directory paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / env.get("DATA_DIR", "data")
LOG_DIR = BASE_DIR / env.get("LOG_DIR", "logs")
REPORT_OUTPUT_DIR = BASE_DIR / env.get("REPORT_OUTPUT_DIR", "reports")

# Ensure required directories exist
for directory in [DATA_DIR, LOG_DIR, REPORT_OUTPUT_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# API Keys & Services
OPENAI_API_KEY = env.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
OPENAI_MODEL = env.get("OPENAI_MODEL", "gpt-4o")

# Nordnet Credentials & API Settings
NORDNET_USERNAME = env.get("NORDNET_USERNAME", "")
NORDNET_PASSWORD = env.get("NORDNET_PASSWORD", "")
NORDNET_SERVICE_KEY = env.get("NORDNET_SERVICE_KEY", "")
NORDNET_ACCOUNT_ID = env.get("NORDNET_ACCOUNT_ID", "12345678")
NORDNET_ENV = env.get("NORDNET_ENV", "sandbox").lower()  # 'sandbox' or 'production'

# Portfolio & Financial Settings
CURRENCY = env.get("CURRENCY", "EUR")

def _get_float(key: str, default: float) -> float:
    val = env.get(key, os.environ.get(key, str(default)))
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def _get_int(key: str, default: int) -> int:
    val = env.get(key, os.environ.get(key, str(default)))
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

INITIAL_BALANCE = _get_float("INITIAL_BALANCE", 10000.0)

# Nordnet Brokerage Fee Tiers
# Taso 3 (1-10 kauppaa/kk): Kotimaa min 7.00 EUR / Ulkomaat min 15.00 EUR (0.15%)
# Taso 2 (11-50 kauppaa/kk): Kotimaa min 5.00 EUR / Ulkomaat min 13.00 EUR (0.10%)
# Taso 1 (>50 kauppaa/kk): Kotimaa min 3.00 EUR / Ulkomaat min 10.00 EUR (0.06%)
NORDNET_FEE_TIER = _get_int("NORDNET_FEE_TIER", 3)

NORDNET_TIERS = {
    1: {"min_eur": 3.00, "min_foreign_eur": 10.00, "percent": 0.0006},
    2: {"min_eur": 5.00, "min_foreign_eur": 13.00, "percent": 0.0010},
    3: {"min_eur": 7.00, "min_foreign_eur": 15.00, "percent": 0.0015},
}

_tier_info = NORDNET_TIERS.get(NORDNET_FEE_TIER, NORDNET_TIERS[3])
COMMISSION_MIN_EUR = _get_float("COMMISSION_MIN_EUR", _tier_info["min_eur"])
COMMISSION_MIN_FOREIGN_EUR = _get_float("COMMISSION_MIN_FOREIGN_EUR", _tier_info["min_foreign_eur"])
COMMISSION_PERCENT = _get_float("COMMISSION_PERCENT", _tier_info["percent"])


def calculate_commission(symbol: str, trade_value: float) -> float:
    """Calculates estimated Nordnet commission based on asset market venue (Domestic vs Foreign vs Nordnet Funds)."""
    if symbol.startswith("NN_"):
        return 0.0  # Nordnet index funds have 0 EUR brokerage fee
    
    if symbol.endswith(".HE"):
        min_fee = COMMISSION_MIN_EUR  # Domestic OMX Helsinki
    else:
        min_fee = COMMISSION_MIN_FOREIGN_EUR  # Foreign exchange (Xetra .DE, LSE .L, US)

    return max(min_fee, round(trade_value * COMMISSION_PERCENT, 2))


def get_min_trade_size_for_symbol(symbol: str, max_fee_ratio: float = 0.025) -> float:
    """Returns minimum recommended trade size in EUR to keep commission below max_fee_ratio (default 2.5%)."""
    if symbol.startswith("NN_"):
        return 50.0  # Minimal size for fee-free index funds
    
    min_fee = COMMISSION_MIN_EUR if symbol.endswith(".HE") else COMMISSION_MIN_FOREIGN_EUR
    return max(MIN_TRADE_EUR, round(min_fee / max_fee_ratio, 2))


# Safety Rails & Fee Thresholds
REBALANCE_INTERVAL_DAYS = _get_int("REBALANCE_INTERVAL_DAYS", 7)
MIN_TRADE_EUR = _get_float("MIN_TRADE_EUR", 200.0)            # Minimum baseline trade size
MAX_POSITION_WEIGHT = _get_float("MAX_POSITION_WEIGHT", 0.20)   # Max 20% weight per stock
TARGET_CASH_PERCENT = _get_float("TARGET_CASH_PERCENT", 0.05)   # 5% cash buffer
STOP_LOSS_PERCENT = _get_float("STOP_LOSS_PERCENT", 0.15)       # 15% stop loss threshold

# Automation & Schedule
WEEKLY_REPORT_DAY = env.get("WEEKLY_REPORT_DAY", "monday").lower()

# Local State & Persistence File Paths
TIUKU_PORTFOLIO_FILE = BASE_DIR / "tiuku_portfolio.json"
HOLDINGS_FILE = DATA_DIR / "holdings.json"
PORTFOLIO_HISTORY_FILE = DATA_DIR / "portfolio_history.json"
REBALANCE_PROPOSALS_FILE = DATA_DIR / "rebalance_proposals.json"
APP_LOG_FILE = LOG_DIR / "app.log"
TRADE_LOG_FILE = LOG_DIR / "trades.log"
