"""
test_quant_analytics.py - Unit and Integration Tests for quant_analytics.py.
Tests:
1. Returns and frequency resampling (daily & weekly smoothing).
2. Risk-adjusted ratios: 30-day Rolling Sharpe & Sortino.
3. Max Drawdown & Time-to-Recovery calculations.
4. Multiple testing corrections: Deflated Sharpe Ratio (DSR) & Bonferroni.
5. Trade attribution metrics: Concentration (% PnL from top 1/2 trades), median holding days, median vs mean return.
6. 10-Portfolio return correlation matrix.
7. End-to-end institutional summary generator.
"""

import math
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

import quant_analytics as qa


@pytest.fixture
def sample_equity_curve():
    """Generates a realistic 100-day daily equity series with known drawdown."""
    dates = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
    # Base path: climbs from 10,000 to 12,000, drops to 9,600 (-20% MDD), then recovers to 12,500
    values = np.linspace(10000, 12000, 30).tolist()
    values += np.linspace(12000, 9600, 20).tolist()   # Drawdown trough
    values += np.linspace(9600, 12500, 50).tolist()   # Recovery
    return pd.Series(values, index=dates, name="test_equity")


@pytest.fixture
def sample_trades_df():
    """Generates a sample trade history DataFrame."""
    return pd.DataFrame({
        "Ticker": ["AAA.HE", "BBB.ST", "CCC", "DDD.HE", "EEE"],
        "Buy Date": ["2026-01-01", "2026-01-05", "2026-01-10", "2026-01-15", "2026-01-20"],
        "Sell Date": ["2026-01-06", "2026-01-15", "2026-01-12", "2026-01-25", "2026-02-10"],
        "Capital Invested": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "Net Return": [1150.0, 900.0, 1050.0, 1400.0, 950.0],
        "Net PnL": [150.0, -100.0, 50.0, 400.0, -50.0],
        "Exit Reason": ["TARGET", "STOP", "TARGET", "TARGET", "STOP"],
    })


def test_calculate_returns_daily_and_weekly(sample_equity_curve):
    r_daily = qa.calculate_returns(sample_equity_curve, freq="D", weekly_smoothing=False)
    assert not r_daily.empty
    assert len(r_daily) == len(sample_equity_curve) - 1

    r_weekly = qa.calculate_returns(sample_equity_curve, weekly_smoothing=True)
    assert not r_weekly.empty
    assert len(r_weekly) < len(r_daily)


def test_rolling_sharpe_and_sortino(sample_equity_curve):
    r_daily = qa.calculate_returns(sample_equity_curve)
    sh = qa.calculate_rolling_sharpe(r_daily, window=30)
    so = qa.calculate_rolling_sortino(r_daily, window=30)

    assert not sh.empty
    assert not so.empty
    assert not np.isnan(sh.dropna().iloc[-1])
    assert not np.isnan(so.dropna().iloc[-1])


def test_max_drawdown_and_recovery(sample_equity_curve):
    mdd, peak, trough = qa.calculate_max_drawdown(sample_equity_curve)
    assert mdd < 0
    # Expected drop is from 12000 to 9600 = (9600 - 12000)/12000 = -0.20 (-20%)
    assert pytest.approx(mdd, 0.01) == -0.20
    assert peak is not None
    assert trough is not None
    assert peak < trough

    rec_days = qa.calculate_time_to_recovery(sample_equity_curve)
    assert rec_days > 0


def test_deflated_sharpe_ratio():
    sharpes = [0.2, 0.5, 0.8, 1.2, 0.4, 0.1, -0.3, 0.6, 0.7, 1.8]
    best_sharpe = 1.8

    report = qa.deflated_sharpe_ratio(
        sharpe=best_sharpe,
        all_sharpes=sharpes,
        nb_trials=10,
        sample_length=252,
    )

    assert "dsr" in report
    assert "is_significant" in report
    assert "bonferroni_p_value" in report
    assert 0.0 <= report["dsr"] <= 1.0
    assert 0.0 <= report["bonferroni_p_value"] <= 1.0


def test_trade_attribution(sample_trades_df):
    attrib = qa.calculate_trade_attribution(sample_trades_df)

    assert attrib["total_trades"] == 5
    assert attrib["win_rate_pct"] == 60.0  # 3 wins out of 5
    assert attrib["total_net_pnl_eur"] == 450.0  # 150 - 100 + 50 + 400 - 50 = 450
    # Top trades: 400 and 150. Gross positive gains = 400 + 150 + 50 = 600.
    # Top-1 concentration: 400 / 600 = 66.7%
    assert pytest.approx(attrib["concentration_top1_pct"], 0.1) == 66.7
    # Top-2 concentration: (400 + 150) / 600 = 550 / 600 = 91.7%
    assert pytest.approx(attrib["concentration_top2_pct"], 0.1) == 91.7
    # Holding days:
    # 2026-01-01 to 2026-01-06 = 5d
    # 2026-01-05 to 2026-01-15 = 10d
    # 2026-01-10 to 2026-01-12 = 2d
    # 2026-01-15 to 2026-01-25 = 10d
    # 2026-01-20 to 2026-02-10 = 21d
    # Sorted days: [2, 5, 10, 10, 21] -> median is 10
    assert attrib["median_holding_days"] == 10.0


def test_correlation_matrix_builder(sample_equity_curve):
    eq_dict = {
        "P1": sample_equity_curve,
        "P2": sample_equity_curve * 1.05,
        "P3": sample_equity_curve * 0.95,
    }
    corr = qa.build_portfolio_correlation_matrix(eq_dict, freq="D")

    assert corr.shape == (3, 3)
    assert pytest.approx(corr.loc["P1", "P1"], 0.01) == 1.0
    assert pytest.approx(corr.loc["P2", "P1"], 0.01) == 1.0


def test_dsr_data_sufficiency_guard():
    sharpes = [0.5, 0.2, -0.1, 0.8, 1.2, 0.4, 0.3, 0.1, -0.2, 0.6]

    # Test with insufficient observations (e.g. T = 2 days)
    short_report = qa.deflated_sharpe_ratio(
        sharpe=1.2,
        all_sharpes=sharpes,
        nb_trials=10,
        sample_length=2,
    )
    assert short_report["has_sufficient_data"] is False
    assert short_report["days_needed"] == 18
    # Null E[max] must NOT explode to 15.74; it must report the stable 1-year baseline (~1.57)
    assert 1.0 <= short_report["expected_max_sharpe"] <= 2.5

    # Test with sufficient observations (T = 50 days)
    suff_report = qa.deflated_sharpe_ratio(
        sharpe=1.2,
        all_sharpes=sharpes,
        nb_trials=10,
        sample_length=50,
    )
    assert suff_report["has_sufficient_data"] is True
    assert suff_report["days_needed"] == 0


def test_get_market_regime():
    # Verify default microcap benchmark is IWC (iShares Micro-Cap ETF)
    regime = qa.get_market_regime(benchmark_ticker="IWC")
    assert "regime" in regime
    assert "badge" in regime
    assert "volatility_20d_pct" in regime
    assert regime["volatility_20d_pct"] > 0
    assert regime["benchmark"] == "IWC"
    assert "iShares Micro-Cap" in regime.get("benchmark_name", "")
    assert regime["vol_low_threshold"] == 20.0
    assert regime["vol_high_threshold"] == 30.0


def test_internal_universe_volatility_calculation():
    # Verify clean handling when universe CSV path does not exist
    vol = qa.calculate_internal_universe_volatility(universe_csv_path=Path("non_existent.csv"))
    assert vol is None

    # Verify clean handling with actual universe file if available
    real_csv = Path(__file__).resolve().parent.parent / "data" / "clean_microcap_universe.csv"
    if real_csv.exists():
        # Test function runs without crashing
        vol_real = qa.calculate_internal_universe_volatility(universe_csv_path=real_csv, sample_size=3)
        if vol_real is not None:
            assert vol_real > 0.0


def test_score_to_star_rating():
    assert qa.score_to_star_rating(14) == "⭐⭐⭐⭐⭐"
    assert qa.score_to_star_rating(11) == "⭐⭐⭐⭐⭐"
    assert qa.score_to_star_rating(10) == "⭐⭐⭐⭐"
    assert qa.score_to_star_rating(8) == "⭐⭐⭐⭐"
    assert qa.score_to_star_rating(7) == "⭐⭐⭐"
    assert qa.score_to_star_rating(5) == "⭐⭐⭐"
    assert qa.score_to_star_rating(4) == "⭐⭐"
    assert qa.score_to_star_rating(1) == "⭐⭐"


def test_calculate_top_picks_conviction(tmp_path):
    import json

    # Create mock portfolio state files
    p1 = tmp_path / "portfolio_P1_Base_state.json"
    p4 = tmp_path / "portfolio_P4_Institutional_state.json"
    p7 = tmp_path / "portfolio_P7_Deep_Value_Extreme_state.json"
    p8 = tmp_path / "portfolio_P8_Quality_Growth_state.json"

    # AAA is held in P1, P4 (+1 liq), P7 (+2 dv), P8 (+1 qual): 4 portfolios + 4 premiums = 8 points (⭐⭐⭐⭐)
    # BBB is held only in P1: 1 portfolio = 1 point (⭐⭐)
    p1.write_text(json.dumps({"positions": [{"Ticker": "AAA"}, {"Ticker": "BBB"}]}))
    p4.write_text(json.dumps({"positions": [{"Ticker": "AAA"}]}))
    p7.write_text(json.dumps({"positions": [{"Ticker": "AAA"}]}))
    p8.write_text(json.dumps({"positions": [{"Ticker": "AAA"}]}))

    df = qa.calculate_top_picks_conviction(tmp_path)
    assert not df.empty
    assert len(df) == 2
    assert df.iloc[0]["Ticker"] == "AAA"
    assert df.iloc[0]["Conviction Score (0-14)"] == 8
    assert df.iloc[0]["Star Rating"] == "⭐⭐⭐⭐"
    assert df.iloc[0]["Held In (count)"] == 4
    assert "Institutional" in df.iloc[0]["Premium Tags (e.g., Institutional, Deep Value)"]
    assert "Deep Value" in df.iloc[0]["Premium Tags (e.g., Institutional, Deep Value)"]
    assert "Quality" in df.iloc[0]["Premium Tags (e.g., Institutional, Deep Value)"]

    assert df.iloc[1]["Ticker"] == "BBB"
    assert df.iloc[1]["Conviction Score (0-14)"] == 1
    assert df.iloc[1]["Star Rating"] == "⭐⭐"


def test_p13_real_market_counterfactual_and_nlp_calibration(tmp_path):
    import json

    # 1. Create mock p13_execution_audit.csv
    exec_csv = tmp_path / "p13_execution_audit.csv"
    exec_csv.write_text(
        "Date,Ticker,Sub_Strategy,Action,Paper_Price,Limit_Price,NextDay_Open,NextDay_Low,NextDay_High,NextDay_Close,Would_Have_Filled,Realistic_Fill_Price,Slippage_vs_Paper_Pct,FX_Fee_EUR,Broker_Fee_EUR\n"
        "2026-10-20,MSAB-B.ST,satellite_nordic_microcap_catalyst,PARTIAL_TP,104.0,103.792,109.5,105.5,109.5,106.0,FILLED,109.5,5.2885,3.21,8.85\n"
        "2026-10-21,EQV1V.HE,core_nordic_midcap_quality,BUY,8.0,8.016,8.02,7.90,8.02,7.92,FILLED,8.016,0.20,0.0,9.0\n",
        encoding="utf-8",
    )

    # 2. Create mock portfolio_P13_Live_Candidate_state.json
    state_json = tmp_path / "portfolio_P13_Live_Candidate_state.json"
    state_json.write_text(
        json.dumps({
            "portfolio_id": "P13_Live_Candidate",
            "starting_balance": 10000.0,
            "cash_balance": 4696.53,
            "positions": [
                {
                    "Ticker": "MSAB-B.ST",
                    "Buy Date": "2026-10-14",
                    "Buy Price": 84.0,
                    "current_price": 108.0,
                    "Currency": "SEK",
                    "Sub_Strategy": "satellite_nordic_microcap_catalyst",
                    "Candidate_Score": 120.0,
                    "Entry_Hard_Facts": {"roe": 25.0, "ebit_margin": 18.0, "rev_yoy": 30.0, "nlp_verdict": "STRONG BUY"},
                },
                {
                    "Ticker": "EQV1V.HE",
                    "Buy Date": "2026-10-21",
                    "Buy Price": 8.0,
                    "current_price": 8.08,
                    "Currency": "EUR",
                    "Sub_Strategy": "core_nordic_midcap_quality",
                    "Candidate_Score": 109.25,
                    "Entry_Hard_Facts": {"roe": 37.84, "ebit_margin": 38.93, "rev_yoy": -8.06, "nlp_verdict": "HOLD"},
                },
            ],
        }),
        encoding="utf-8",
    )

    # 3. Create mock p13_shadow_audit_log.jsonl with 2 snapshots
    shadow_jsonl = tmp_path / "p13_shadow_audit_log.jsonl"
    entry1 = {
        "timestamp": "2026-10-20T08:00:00+00:00",
        "regime": {"iwc_price": 174.0, "sma_50": 190.0, "is_choppy_or_bear": True, "required_cash_buffer_eur": 4000.0},
        "portfolio_snapshot": {"cash_balance": 5756.96, "non_hodl_stock_value_eur": 4467.95, "total_p13_equity_eur": 10224.91},
        "candidates_ranking": {
            "core_nordic_midcap_quality": {
                "status": "BLOCKED_BY_REGIME_CASH_BUFFER",
                "top_5_candidates": [
                    {
                        "rank": 1,
                        "ticker": "MIPS.ST",
                        "candidate_score": 111.0,
                        "price": 400.0,
                        "currency": "SEK",
                        "hard_facts": {"roe": 29.0, "ebit_margin": 46.0, "rev_yoy": 10.0, "nlp_verdict": "HOLD"},
                        "selected": False,
                        "selection_status": "BLOCKED_BY_REGIME_CASH_BUFFER",
                    }
                ],
            }
        },
        "open_positions_health": [
            {
                "ticker": "MSAB-B.ST",
                "sub_strategy": "satellite_nordic_microcap_catalyst",
                "current_price": 104.0,
                "pnl_pct": 23.81,
                "drawdown_from_peak_pct": 2.8,
                "distance_to_stop_pct": 28.9,
                "distance_to_partial_tp_pct": 0.96,
                "trading_days_held": 4,
                "nlp_status": "STRONG BUY",
            }
        ],
    }
    entry2 = {
        "timestamp": "2026-10-25T20:00:00+00:00",
        "regime": {"iwc_price": 176.36, "sma_50": 188.4, "is_choppy_or_bear": True, "required_cash_buffer_eur": 4000.0},
        "portfolio_snapshot": {"cash_balance": 4696.53, "non_hodl_stock_value_eur": 5620.99, "total_p13_equity_eur": 10317.52},
        "candidates_ranking": {
            "core_nordic_midcap_quality": {
                "status": "INSUFFICIENT_CASH",
                "top_5_candidates": [
                    {
                        "rank": 1,
                        "ticker": "MIPS.ST",
                        "candidate_score": 111.0,
                        "price": 392.0,
                        "currency": "SEK",
                        "hard_facts": {"roe": 29.0, "ebit_margin": 46.0, "rev_yoy": 10.0, "nlp_verdict": "HOLD"},
                        "selected": False,
                        "selection_status": "BLOCKED_BY_REGIME_CASH_BUFFER",
                    }
                ],
            }
        },
        "open_positions_health": [
            {
                "ticker": "MSAB-B.ST",
                "sub_strategy": "satellite_nordic_microcap_catalyst",
                "current_price": 108.0,
                "pnl_pct": 28.57,
                "drawdown_from_peak_pct": 0.92,
                "distance_to_stop_pct": 14.21,
                "distance_to_partial_tp_pct": 0.0,
                "trading_days_held": 8,
                "nlp_status": "STRONG BUY",
            }
        ],
    }
    shadow_jsonl.write_text(json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n", encoding="utf-8")

    # Verify load_portfolio_data ingests P13 shadow equity curve
    eq_dict, _ = qa.load_portfolio_data(tmp_path)
    assert "P13_Live_Candidate" in eq_dict
    assert len(eq_dict["P13_Live_Candidate"]) == 2
    assert pytest.approx(float(eq_dict["P13_Live_Candidate"].iloc[-1]), 0.01) == 10317.52

    # Run counterfactual & real-market audit
    res = qa.analyze_p13_real_market_and_counterfactual(tmp_path, fetch_live_prices=False, use_llm=False)

    exec_sum = res["execution_summary"]
    assert exec_sum["total_orders"] == 2
    assert exec_sum["filled_orders"] == 2
    assert exec_sum["fill_rate_pct"] == 100.0
    assert pytest.approx(exec_sum["total_broker_fees_eur"], 0.01) == 17.85

    cf_sum = res["counterfactual_summary"]
    # Selected (MSAB-B.ST +28.57%, EQV1V.HE +1.00%) -> mean +14.79%
    # Blocked (MIPS.ST 400 -> 392 = -2.00%)
    assert cf_sum["selected_mean_return_pct"] > 10.0
    assert cf_sum["blocked_mean_return_pct"] == -2.0
    assert cf_sum["guard_alpha_saved_pct"] > 12.0

    mfe_df = res["mfe_mae_df"]
    assert not mfe_df.empty
    assert mfe_df.iloc[0]["Ticker"] == "MSAB-B.ST"
    assert mfe_df.iloc[0]["MFE_Peak_PnL_Pct"] == 28.57
    assert mfe_df.iloc[0]["MAE_Worst_PnL_Pct"] == 23.81

    pm = res["ai_post_mortem"]
    assert pm["readiness_score"] >= 65
    assert len(pm["position_critiques"]) == 2
    # EQV1V.HE has rev_yoy = -8.06 -> Devil's advocate should warn about shrinking revenue
    eqv_crit = next(c for c in pm["position_critiques"] if c["Ticker"] == "EQV1V.HE")
    assert "supistuu" in eqv_crit["Devils_Advocate_Challenge"]



