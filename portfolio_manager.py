"""
Portfolio Manager root proxy module.
"""
from screener.portfolio_manager import (
    PortfolioManager,
    execute_trade_signal,
    update_portfolio,
    open_position,
    update_trailing_stops,
    evaluate_position,
    calculate_atr,
    STARTING_VIRTUAL_EQUITY,
    POSITION_ALLOCATION,
    CATASTROPHIC_STOP_PCT,
    TRAILING_STOP_PCT,
    DEFAULT_ATR_PERIOD,
    DEFAULT_ATR_MULTIPLIER,
)

__all__ = [
    "PortfolioManager",
    "execute_trade_signal",
    "update_portfolio",
    "open_position",
    "update_trailing_stops",
    "evaluate_position",
    "calculate_atr",
    "STARTING_VIRTUAL_EQUITY",
    "POSITION_ALLOCATION",
    "CATASTROPHIC_STOP_PCT",
    "TRAILING_STOP_PCT",
    "DEFAULT_ATR_PERIOD",
    "DEFAULT_ATR_MULTIPLIER",
]
