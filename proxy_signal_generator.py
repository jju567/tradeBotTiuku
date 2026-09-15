"""
Direct entry point for Historical Proxy-Signal Generator (Pump Hunter).
"""

from screener.proxy_signal_generator import (
    HistoricalProxySignalGenerator,
    ProxySignal,
    DEFAULT_HELSINKI_UNIVERSE,
    main,
)

__all__ = [
    "HistoricalProxySignalGenerator",
    "ProxySignal",
    "DEFAULT_HELSINKI_UNIVERSE",
    "main",
]

if __name__ == "__main__":
    main()
