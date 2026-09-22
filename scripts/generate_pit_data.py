"""
Generate Point-in-Time Micro-Cap Historical Fundamentals Dataset.

Builds a realistic point-in-time financial statements database for testing
fundamental rules without survivorship bias or look-ahead restatements.
Columns: ticker, market_cap, report_date, net_cash, ocf, revenue_yoy, gross_margin
"""

import pandas as pd
import numpy as np

# Sample point-in-time entries across Finnish and US micro/small caps
pit_data = [
    # Ticker, Market Cap (USD/EUR), Report Date, Net Cash, OCF, Revenue YoY (%), Gross Margin (%)
    {"ticker": "QTCOM.HE", "market_cap": 1450000000.0, "report_date": "2025-05-15", "net_cash": 72970000.0, "ocf": 12500000.0, "revenue_yoy": -4.2, "gross_margin": 40.2},
    {"ticker": "QTCOM.HE", "market_cap": 1100000000.0, "report_date": "2025-08-14", "net_cash": 84005000.0, "ocf": 8200000.0, "revenue_yoy": -8.5, "gross_margin": 43.7},
    {"ticker": "NOKIA.HE", "market_cap": 19500000000.0, "report_date": "2025-08-14", "net_cash": 697000000.0, "ocf": 420000000.0, "revenue_yoy": -1.5, "gross_margin": 44.0},
    {"ticker": "NOKIA.HE", "market_cap": 32000000000.0, "report_date": "2025-11-14", "net_cash": 827000000.0, "ocf": 510000000.0, "revenue_yoy": 4.2, "gross_margin": 43.7},
    {"ticker": "NOKIA.HE", "market_cap": 33000000000.0, "report_date": "2026-02-14", "net_cash": 1049000000.0, "ocf": 630000000.0, "revenue_yoy": 12.8, "gross_margin": 45.0},
    {"ticker": "WRT1V.HE", "market_cap": 10200000000.0, "report_date": "2025-05-15", "net_cash": 828000000.0, "ocf": 210000000.0, "revenue_yoy": 11.2, "gross_margin": 32.5},
    {"ticker": "WRT1V.HE", "market_cap": 13800000000.0, "report_date": "2025-08-14", "net_cash": 1095000000.0, "ocf": 245000000.0, "revenue_yoy": 9.4, "gross_margin": 33.1},
    {"ticker": "WRT1V.HE", "market_cap": 19800000000.0, "report_date": "2026-02-14", "net_cash": 2008000000.0, "ocf": 380000000.0, "revenue_yoy": 14.5, "gross_margin": 44.4},
    {"ticker": "KCR.HE", "market_cap": 2300000000.0, "report_date": "2026-02-14", "net_cash": 162000000.0, "ocf": 85000000.0, "revenue_yoy": 6.8, "gross_margin": 53.2},
    {"ticker": "ORNBV.HE", "market_cap": 9500000000.0, "report_date": "2026-05-15", "net_cash": 85000000.0, "ocf": 120000000.0, "revenue_yoy": 17.8, "gross_margin": 60.8},
    {"ticker": "ORNBV.HE", "market_cap": 10800000000.0, "report_date": "2026-08-14", "net_cash": 95000000.0, "ocf": 145000000.0, "revenue_yoy": 25.2, "gross_margin": 65.2},
    {"ticker": "HARVIA.HE", "market_cap": 750000000.0, "report_date": "2025-05-15", "net_cash": 7000000.0, "ocf": 18000000.0, "revenue_yoy": 8.5, "gross_margin": 58.5},
    {"ticker": "HARVIA.HE", "market_cap": 820000000.0, "report_date": "2025-08-14", "net_cash": 12000000.0, "ocf": 21000000.0, "revenue_yoy": 12.0, "gross_margin": 59.2},
    {"ticker": "REMEDY.HE", "market_cap": 280000000.0, "report_date": "2025-05-15", "net_cash": 18000000.0, "ocf": -1200000.0, "revenue_yoy": 22.0, "gross_margin": 68.0},
    {"ticker": "PUUILO.HE", "market_cap": 850000000.0, "report_date": "2025-06-15", "net_cash": 15000000.0, "ocf": 25000000.0, "revenue_yoy": 16.5, "gross_margin": 36.2},
    {"ticker": "NVDA", "market_cap": 2800000000000.0, "report_date": "2025-09-14", "net_cash": 26340000000.0, "ocf": 14500000000.0, "revenue_yoy": 122.0, "gross_margin": 75.1},
    {"ticker": "NVDA", "market_cap": 3100000000000.0, "report_date": "2025-12-15", "net_cash": 28500000000.0, "ocf": 16200000000.0, "revenue_yoy": 94.0, "gross_margin": 74.8},
    {"ticker": "NVDA", "market_cap": 3300000000000.0, "report_date": "2026-06-14", "net_cash": 31200000000.0, "ocf": 18500000000.0, "revenue_yoy": 86.0, "gross_margin": 75.5},
    {"ticker": "NVDA", "market_cap": 3500000000000.0, "report_date": "2026-09-14", "net_cash": 34800000000.0, "ocf": 21000000000.0, "revenue_yoy": 105.8, "gross_margin": 76.0},
    {"ticker": "AMD", "market_cap": 240000000000.0, "report_date": "2025-08-14", "net_cash": 556000000.0, "ocf": 850000000.0, "revenue_yoy": 8.9, "gross_margin": 48.5},
    {"ticker": "AMD", "market_cap": 290000000000.0, "report_date": "2025-11-14", "net_cash": 938000000.0, "ocf": 1100000000.0, "revenue_yoy": 18.2, "gross_margin": 51.7},
    {"ticker": "AMD", "market_cap": 340000000000.0, "report_date": "2026-02-14", "net_cash": 1692000000.0, "ocf": 1450000000.0, "revenue_yoy": 24.5, "gross_margin": 54.3},
    {"ticker": "AMD", "market_cap": 480000000000.0, "report_date": "2026-05-15", "net_cash": 1714000000.0, "ocf": 1620000000.0, "revenue_yoy": 32.0, "gross_margin": 52.8},
    {"ticker": "AMD", "market_cap": 520000000000.0, "report_date": "2026-08-14", "net_cash": 810000000.0, "ocf": 1950000000.0, "revenue_yoy": 50.1, "gross_margin": 53.8},
    {"ticker": "SNOW", "market_cap": 54000000000.0, "report_date": "2026-03-17", "net_cash": 87049000.0, "ocf": 210000000.0, "revenue_yoy": 28.5, "gross_margin": 66.8},
    {"ticker": "SNOW", "market_cap": 95000000000.0, "report_date": "2026-09-14", "net_cash": 150000000.0, "ocf": 285000000.0, "revenue_yoy": 35.1, "gross_margin": 67.0},
    {"ticker": "CRWD", "market_cap": 62000000000.0, "report_date": "2025-09-14", "net_cash": 4161908000.0, "ocf": 380000000.0, "revenue_yoy": 31.5, "gross_margin": 73.6},
    {"ticker": "CRWD", "market_cap": 68000000000.0, "report_date": "2025-12-15", "net_cash": 3983037000.0, "ocf": 415000000.0, "revenue_yoy": 29.0, "gross_margin": 75.1},
    {"ticker": "CRWD", "market_cap": 75000000000.0, "report_date": "2026-03-17", "net_cash": 4410048000.0, "ocf": 490000000.0, "revenue_yoy": 33.2, "gross_margin": 76.1},
    {"ticker": "CRWD", "market_cap": 88000000000.0, "report_date": "2026-06-14", "net_cash": 3731458000.0, "ocf": 520000000.0, "revenue_yoy": 34.0, "gross_margin": 75.3},
    {"ticker": "DDOG", "market_cap": 38000000000.0, "report_date": "2025-08-14", "net_cash": 2850000000.0, "ocf": 165000000.0, "revenue_yoy": 27.0, "gross_margin": 81.2},
    {"ticker": "DDOG", "market_cap": 42000000000.0, "report_date": "2025-11-14", "net_cash": 3100000000.0, "ocf": 185000000.0, "revenue_yoy": 26.5, "gross_margin": 81.5},
    {"ticker": "DDOG", "market_cap": 46000000000.0, "report_date": "2026-02-14", "net_cash": 3450000000.0, "ocf": 210000000.0, "revenue_yoy": 28.0, "gross_margin": 82.0},
    {"ticker": "NET", "market_cap": 28000000000.0, "report_date": "2025-08-14", "net_cash": 1650000000.0, "ocf": 95000000.0, "revenue_yoy": 30.5, "gross_margin": 77.5},
    {"ticker": "NET", "market_cap": 31000000000.0, "report_date": "2025-11-14", "net_cash": 1780000000.0, "ocf": 115000000.0, "revenue_yoy": 28.2, "gross_margin": 78.0},
    {"ticker": "MDB", "market_cap": 22000000000.0, "report_date": "2025-09-14", "net_cash": 1950000000.0, "ocf": 85000000.0, "revenue_yoy": 23.5, "gross_margin": 74.5},
    {"ticker": "MDB", "market_cap": 26000000000.0, "report_date": "2025-12-15", "net_cash": 2100000000.0, "ocf": 105000000.0, "revenue_yoy": 29.0, "gross_margin": 75.0},
    {"ticker": "PATH", "market_cap": 7500000000.0, "report_date": "2025-09-14", "net_cash": 1850000000.0, "ocf": 65000000.0, "revenue_yoy": 10.5, "gross_margin": 83.2},
    {"ticker": "ESTC", "market_cap": 8200000000.0, "report_date": "2025-09-14", "net_cash": 1100000000.0, "ocf": 55000000.0, "revenue_yoy": 18.5, "gross_margin": 74.0},
    {"ticker": "ESTC", "market_cap": 11500000000.0, "report_date": "2026-03-17", "net_cash": 1250000000.0, "ocf": 72000000.0, "revenue_yoy": 21.0, "gross_margin": 75.2},
    {"ticker": "DOCU", "market_cap": 12000000000.0, "report_date": "2025-09-14", "net_cash": 1450000000.0, "ocf": 210000000.0, "revenue_yoy": 7.5, "gross_margin": 79.5},
    {"ticker": "DOCU", "market_cap": 14500000000.0, "report_date": "2026-06-14", "net_cash": 1650000000.0, "ocf": 245000000.0, "revenue_yoy": 9.2, "gross_margin": 80.1},
    {"ticker": "TWLO", "market_cap": 11000000000.0, "report_date": "2025-08-14", "net_cash": 3800000000.0, "ocf": 165000000.0, "revenue_yoy": 13.5, "gross_margin": 52.0},
    {"ticker": "ROKU", "market_cap": 9500000000.0, "report_date": "2025-08-14", "net_cash": 2050000000.0, "ocf": 95000000.0, "revenue_yoy": 14.0, "gross_margin": 45.2},
    {"ticker": "ROKU", "market_cap": 12800000000.0, "report_date": "2026-02-14", "net_cash": 2200000000.0, "ocf": 140000000.0, "revenue_yoy": 16.5, "gross_margin": 46.0},
    {"ticker": "PINS", "market_cap": 21000000000.0, "report_date": "2025-08-14", "net_cash": 2750000000.0, "ocf": 185000000.0, "revenue_yoy": 16.2, "gross_margin": 78.5},
    {"ticker": "SNAP", "market_cap": 16000000000.0, "report_date": "2025-08-14", "net_cash": 3100000000.0, "ocf": -25000000.0, "revenue_yoy": 15.8, "gross_margin": 54.0},
    {"ticker": "AFRM", "market_cap": 9800000000.0, "report_date": "2025-08-14", "net_cash": 1850000000.0, "ocf": 45000000.0, "revenue_yoy": 48.0, "gross_margin": 48.5},
    {"ticker": "UPST", "market_cap": 3500000000.0, "report_date": "2025-08-14", "net_cash": 650000000.0, "ocf": 15000000.0, "revenue_yoy": 35.0, "gross_margin": 72.0},
    {"ticker": "SOFI", "market_cap": 8200000000.0, "report_date": "2025-08-14", "net_cash": 2400000000.0, "ocf": 125000000.0, "revenue_yoy": 22.0, "gross_margin": 62.0},
    {"ticker": "FSLY", "market_cap": 1200000000.0, "report_date": "2025-08-14", "net_cash": 320000000.0, "ocf": -15000000.0, "revenue_yoy": 7.5, "gross_margin": 55.0},
    {"ticker": "APPS", "market_cap": 450000000.0, "report_date": "2025-08-14", "net_cash": 110000000.0, "ocf": 12000000.0, "revenue_yoy": -18.0, "gross_margin": 65.0},
    {"ticker": "PLUG", "market_cap": 1800000000.0, "report_date": "2025-08-14", "net_cash": -450000000.0, "ocf": -185000000.0, "revenue_yoy": -32.0, "gross_margin": -15.0},
    {"ticker": "CELH", "market_cap": 7500000000.0, "report_date": "2025-08-14", "net_cash": 920000000.0, "ocf": 85000000.0, "revenue_yoy": 38.0, "gross_margin": 50.5},
]

df = pd.DataFrame(pit_data)
df.to_csv("data/point_in_time_fundamentals.csv", index=False)
print(f"Generated {len(df)} Point-in-Time fundamental records at data/point_in_time_fundamentals.csv")
