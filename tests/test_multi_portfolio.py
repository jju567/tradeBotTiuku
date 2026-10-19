"""
tests/test_multi_portfolio.py - Tests for Multi-Portfolio Configuration, Market Engine, and Email Notifier.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import yaml
import pytest
import pandas as pd

from email_notifier import send_portfolio_alert
from main_controller import (
    PortfolioConfig,
    PortfolioInstance,
    MarketDataEngine,
    MasterLiveTradingDaemon,
    DEFAULT_PORTFOLIOS_YAML,
)


def test_portfolios_yaml_structure():
    """Verify that portfolios_config.yaml exists and contains all 10 expected portfolios with valid parameters."""
    assert DEFAULT_PORTFOLIOS_YAML.exists()
    with open(DEFAULT_PORTFOLIOS_YAML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert "portfolios" in data
    portfolios = data["portfolios"]
    assert len(portfolios) == 13

    expected_portfolios = [
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
        "P11_Meta_Consensus",
        "P12_Momentum_Breakout",
    ]
    for p in expected_portfolios:
        assert p in portfolios, f"Missing portfolio {p}"
        cfg = portfolios[p]
        assert "strategy" in cfg
        assert "slots" in cfg
        assert "start_cash" in cfg
        assert cfg["start_cash"] == 10000

    # Specific checks for P10_Micro_Sniper
    p10 = portfolios["P10_Micro_Sniper"]
    assert p10["slots"] == 5
    assert p10["slot_size"] == 2000

    # Specific checks for P11_Meta_Consensus
    p11 = portfolios["P11_Meta_Consensus"]
    assert p11["strategy"] == "Meta_Consensus"
    assert p11["slots"] == 5
    assert p11["slot_size"] == 2000
    assert p11["min_conviction_score"] == 8
    assert p11["min_exit_conviction_score"] == 6
    assert "Institutional" in p11["required_tags"]

    # Specific checks for P12_Momentum_Breakout
    p12 = portfolios["P12_Momentum_Breakout"]
    assert p12["strategy"] == "Momentum_Breakout"
    assert p12["slots"] == 5
    assert p12["slot_size"] == 2000
    assert p12["volume_surge_multiplier"] == 3.0
    assert p12["min_price_change_pct"] == 2.0
    assert p12["min_adv_20d"] == 500000
    assert p12["trailing_stop_pct"] == 0.06
    assert p12["max_holding_days"] == 21
    assert p12["require_nlp_clean"] is True

    # Specific checks for P13_Live_Candidate (70% Mid-Cap Core + 30% Micro-Cap Satellite)
    assert "P13_Live_Candidate" in portfolios
    p13 = portfolios["P13_Live_Candidate"]
    assert p13["description"] == "Live 10k € OST Hybrid: 70% Nordic Mid-Cap Core + 30% US/Nordic Micro-Cap Satellite"
    assert p13["start_cash"] == 10000
    assert p13.get("ignore_hodl_positions") is True
    assert "sub_strategies" in p13
    assert "core_nordic_midcap_quality" in p13["sub_strategies"]
    assert "satellite_us_microcap_growth" in p13["sub_strategies"]
    assert "satellite_nordic_microcap_catalyst" in p13["sub_strategies"]
    assert "satellite_p12_momentum" not in p13["sub_strategies"]

    core_sub = p13["sub_strategies"]["core_nordic_midcap_quality"]
    assert core_sub["allocated_capital"] == 7000
    assert core_sub["slots"] == 3
    assert core_sub["slot_sizes"] == [2350, 2350, 2300]
    assert core_sub["universe_filter"] == "Nordic_MidCap_Compounder"

    us_sub = p13["sub_strategies"]["satellite_us_microcap_growth"]
    assert us_sub["allocated_capital"] == 1500
    assert us_sub["slots"] == 1
    assert us_sub["slot_sizes"] == [1500]
    assert us_sub["universe_filter"] == "US_MicroCap_Growth"
    assert any("Dividend_Yield: 0.0" in r for r in us_sub["rules"])

    nordic_sub = p13["sub_strategies"]["satellite_nordic_microcap_catalyst"]
    assert nordic_sub["allocated_capital"] == 1500
    assert nordic_sub["slots"] == 1
    assert nordic_sub["slot_sizes"] == [1500]
    assert nordic_sub["universe_filter"] == "Nordic_MicroCap_Catalyst"

    assert p13["execution_constraints"]["allowed_order_types"] == ["LIMIT"]
    assert p13["execution_constraints"]["market_regime_cash_buffer_pct"] == 40.0
    assert p13["execution_constraints"]["portfolio_max_drawdown_alert_pct"] == -15.0

    # Also check PortfolioConfig.from_dict mapping
    cfg13 = PortfolioConfig.from_dict("P13_Live_Candidate", p13)
    assert cfg13.portfolio_id == "P13_Live_Candidate"
    assert cfg13.start_cash == 10000.0
    assert cfg13.slots == 5  # 3 core + 1 US + 1 Nordic
    assert cfg13.order_type == "LIMIT"
    assert cfg13.limit_buffer_pct == pytest.approx(0.002)
    assert cfg13.market_regime_cash_buffer_pct == pytest.approx(0.40)
    assert cfg13.ignore_hodl_positions is True
    assert cfg13.portfolio_max_drawdown_alert_pct == pytest.approx(-15.0)
    assert "core_nordic_midcap_quality" in cfg13.sub_strategies
    assert "satellite_us_microcap_growth" in cfg13.sub_strategies
    assert "satellite_nordic_microcap_catalyst" in cfg13.sub_strategies




def test_portfolio_instance_creation(tmp_path):
    """Test PortfolioInstance state initialization, save, load, and snapshot."""
    cfg = PortfolioConfig(
        portfolio_id="P_Test",
        strategy="Profile B",
        dead_money_days=180,
        min_adv=50000.0,
        slots=10,
        start_cash=10000.0,
        regions=["FI", "SE"],
    )
    inst = PortfolioInstance(cfg, base_dir=tmp_path)
    assert inst.cash_balance == 10000.0
    assert inst.currency == "EUR"
    assert inst.state_file.exists()
    assert inst.history_file.exists()

    # Append dummy trade
    trade = {
        "Ticker": "TEST.HE",
        "Buy Date": "2026-09-01",
        "Sell Date": "2026-09-20",
        "Buy Price": 10.0,
        "Sell Price": 12.0,
        "Shares": 100,
        "Capital Invested": 1009.0,
        "Gross Sale Value": 1200.0,
        "Transaction Fee": 9.0,
        "Net Return": 1191.0,
        "Net PnL": 182.0,
        "Exit Reason": "TAKE_PROFIT",
    }
    inst.append_trade(trade)
    trades = inst.load_trade_history()
    assert len(trades) == 1
    assert trades[0]["Ticker"] == "TEST.HE"


def test_email_notifier_graceful_failure():
    """Verify that email notifier handles socket/SMTP failure gracefully without raising."""
    with patch("smtplib.SMTP", side_effect=Exception("SMTP Connection Refused")):
        with patch("email_notifier.load_env_credentials", return_value={
            "SMTP_SERVER": "invalid.smtp.host",
            "SMTP_PORT": "587",
            "SMTP_USER": "test@user.com",
            "SMTP_PASS": "secret",
            "ALERT_EMAIL": "alert@user.com",
        }):
            res = send_portfolio_alert(
                portfolio_id="P5_Nordic_Only",
                event_type="BUY",
                ticker="EXEL.HE",
                details={"shares": 50, "price": 13.40},
            )
            assert res is False


def test_permanent_nlp_decision_archive(tmp_path):
    """Verify that append_nlp_decision writes correct CSV format and handles headers properly."""
    from main_controller import append_nlp_decision

    test_archive = tmp_path / "nlp_decisions_archive.csv"
    assert not test_archive.exists()

    # Append first decision
    append_nlp_decision(
        ticker="BIOA.ST",
        headline="BioArctic presents positive Phase 3 clinical data",
        llm_decision="HOLD",
        reasoning="Positive drug trial progression, no dilution",
        archive_path=test_archive,
        timestamp="2026-09-23T11:00:00+00:00",
    )
    assert test_archive.exists()

    # Append second decision
    append_nlp_decision(
        ticker="SEZI.ST",
        headline="Seafire AB beslutar om företrädesemission av aktier",
        llm_decision="REJECT",
        reasoning="Fatal red flag företrädesemission detected",
        archive_path=test_archive,
        timestamp="2026-09-23T11:05:00+00:00",
    )

    # Attempt to append empty headlines (should be strictly skipped)
    append_nlp_decision(
        ticker="EMPTY.ST",
        headline="",
        llm_decision="HOLD",
        reasoning="Empty headline",
        archive_path=test_archive,
    )
    append_nlp_decision(
        ticker="BLANK.ST",
        headline="    \n   ",
        llm_decision="HOLD",
        reasoning="Whitespace headline",
        archive_path=test_archive,
    )

    with open(test_archive, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    # Empty headlines were skipped, line count remains 3 (header + 2 valid rows)
    assert len(lines) == 3
    assert lines[0] == "Timestamp,Ticker,Headline,LLM_Decision,Reasoning"
    assert "BIOA.ST" in lines[1]
    assert "HOLD" in lines[1]
    assert "SEZI.ST" in lines[2]
    assert "REJECT" in lines[2]


def test_market_engine_nlp_caching_and_deduplication(tmp_path):
    """Verify that MarketDataEngine evaluates and logs new headlines EXACTLY ONCE globally."""
    archive_file = tmp_path / "test_nlp_archive.csv"
    engine = MarketDataEngine()
    engine.evaluated_news_cache.clear()

    mock_news = [
        {"content": {"title": "Company reports record Q2 profit beat", "summary": "Strong sales"}},
        {"content": {"title": "", "summary": ""}},  # Empty item: must be skipped
        {"title": None, "summary": None},           # Malformed/empty: must be skipped
    ]

    with patch("main_controller.DEFAULT_NLP_ARCHIVE_CSV", archive_file):
        with patch("yfinance.Ticker") as mock_ticker:
            mock_inst = MagicMock()
            mock_inst.news = mock_news
            mock_ticker.return_value = mock_inst

            # First evaluation: new headline -> should log to CSV ONCE
            v1, r1 = engine._evaluate_news_radar("ABC")
            assert v1 in ("HOLD", "WARN", "REJECT")
            assert archive_file.exists()

            with open(archive_file, "r", encoding="utf-8") as f:
                lines_first = [line.strip() for line in f.readlines() if line.strip()]
            assert len(lines_first) == 2  # Header + 1 record

            # Second evaluation for the same ticker/headline (e.g. subsequent portfolio or cycle)
            v2, r2 = engine._evaluate_news_radar("ABC")
            assert (v2, r2) == (v1, r1)

            with open(archive_file, "r", encoding="utf-8") as f:
                lines_second = [line.strip() for line in f.readlines() if line.strip()]
            # Must NOT append duplicate line — exactly 2 lines remain
            assert len(lines_second) == 2



def test_send_run_summary_email():
    """Verify that send_run_summary_email formats summary and sends single consolidated email."""
    from email_notifier import send_run_summary_email

    run_trades = [
        {
            "portfolio_id": "P6_US_Only",
            "action": "BUY",
            "ticker": "IDN",
            "details": {
                "strategy": "Profile B",
                "shares": 465,
                "buy_price": "2.43 USD",
                "total_cost": "1,140.23 USD (997.93 EUR)",
                "remaining_cash": "2,023.36 EUR",
            },
        },
        {
            "portfolio_id": "P1_Base",
            "action": "SELL",
            "ticker": "WRAP",
            "details": {
                "exit_reason": "TAKE_PROFIT",
                "shares": 100,
                "sell_price": "5.50 USD",
                "net_pnl": "+250.00 USD",
            },
        }
    ]
    portfolio_results = {
        "P1_Base": {"positions_count": 5, "cash": 4500.0, "total_equity": 10250.0, "return_pct": 2.5},
        "P6_US_Only": {"positions_count": 8, "cash": 2023.36, "total_equity": 10100.0, "return_pct": 1.0},
    }

    import email
    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value = mock_instance

        with patch("email_notifier.load_env_credentials", return_value={
            "SMTP_SERVER": "smtp.mock.server",
            "SMTP_PORT": "587",
            "SMTP_USER": "test@user.com",
            "SMTP_PASS": "secret",
            "ALERT_EMAIL": "alert@user.com",
        }):
            res = send_run_summary_email(run_trades, portfolio_results)
            assert res is True
            assert mock_instance.sendmail.called
            sent_args = mock_instance.sendmail.call_args[0]
            raw_msg = email.message_from_string(sent_args[2])
            body_parts = []
            for part in raw_msg.walk():
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode("utf-8", errors="ignore"))
            full_body = "\n".join(body_parts)

            assert "P6_US_Only" in full_body
            assert "IDN" in full_body
            assert "P1_Base" in full_body
            assert "WRAP" in full_body
            assert "Ajon Kauppayhteenveto" in full_body


def test_meta_consensus_execution(tmp_path):
    """
    Test P11_Meta_Consensus logic:
    1. Only buys when Conviction Score >= 8 and has Institutional + (Deep Value OR Quality Growth).
    2. Uses slot_size (~2,000 €).
    3. Exits when Conviction Score drops < 6.
    """
    import pandas as pd

    # Mock MasterLiveTradingDaemon instance with mock portfolios
    yaml_file = tmp_path / "portfolios.yaml"
    yaml_content = """
portfolios:
  P11_Meta_Consensus:
    strategy: "Meta_Consensus"
    slots: 5
    slot_size: 2000
    start_cash: 10000
    min_conviction_score: 8
    min_exit_conviction_score: 6
    required_tags: ["Institutional"]
    regions: ["US"]
"""
    yaml_file.write_text(yaml_content, encoding="utf-8")

    daemon = MasterLiveTradingDaemon(config_yaml_path=yaml_file)
    p11 = daemon.portfolios[0]
    p11.portfolio_dir = tmp_path / "portfolios"
    p11.portfolio_dir.mkdir(parents=True, exist_ok=True)
    p11.state_file = p11.portfolio_dir / "portfolio_P11_Meta_Consensus_state.json"
    p11.history_file = p11.portfolio_dir / "portfolio_P11_Meta_Consensus_history.csv"
    p11.ensure_history_csv()
    p11.positions = []
    p11.cash_balance = 10000.0
    p11.save_state()

    # Setup market data
    daemon.market_engine.prices_cache = {"TOP1": 50.0, "LOW1": 10.0, "NO_TAG": 20.0}
    daemon.market_engine.universe_metadata = {
        "TOP1": {"market": "US", "currency": "USD", "adv_20d_local": 1000000.0, "adv_20d_usd": 1000000.0, "current_price": 50.0},
        "LOW1": {"market": "US", "currency": "USD", "adv_20d_local": 1000000.0, "adv_20d_usd": 1000000.0, "current_price": 10.0},
        "NO_TAG": {"market": "US", "currency": "USD", "adv_20d_local": 1000000.0, "adv_20d_usd": 1000000.0, "current_price": 20.0},
    }
    daemon.market_engine.fx_rates = {"USD": 1.05, "EUR": 1.0}

    # Mock calculate_top_picks_conviction returning candidates
    mock_df_conviction = pd.DataFrame([
        {
            "Ticker": "TOP1",
            "Conviction Score (0-14)": 9,
            "Star Rating": "⭐⭐⭐⭐",
            "Held In (count)": 6,
            "Premium Tags (e.g., Institutional, Deep Value)": "💧 Institutional (+1), 💎 Deep Value (+2)",
            "Portfolios": "P1_Base, P4_Institutional, P7_Deep_Value_Extreme",
        },
        {
            "Ticker": "LOW1",
            "Conviction Score (0-14)": 4,  # Score < 8, should be skipped
            "Star Rating": "⭐⭐",
            "Held In (count)": 3,
            "Premium Tags (e.g., Institutional, Deep Value)": "💧 Institutional (+1)",
            "Portfolios": "P1_Base, P4_Institutional",
        },
        {
            "Ticker": "NO_TAG",
            "Conviction Score (0-14)": 8,  # Score >= 8, but missing Deep Value / Quality Growth
            "Star Rating": "⭐⭐⭐⭐",
            "Held In (count)": 7,
            "Premium Tags (e.g., Institutional, Deep Value)": "💧 Institutional (+1)",
            "Portfolios": "P1, P2, P3, P4, P5, P6, P10",
        },
    ])

    with patch("main_controller.calculate_top_picks_conviction", return_value=mock_df_conviction):
        new_buys = daemon.execute_phase2_screening(p11)
        assert new_buys == 1
        assert len(p11.positions) == 1
        assert p11.positions[0]["Ticker"] == "TOP1"
        assert p11.positions[0]["Strategy"] == "Meta_Consensus"
        # Slot size ~2,000 EUR in USD (~2,100 USD) / $50 = ~42 shares
        assert p11.positions[0]["Shares"] > 35

        # Test Exit: conviction score drops < 6
        mock_dropped_conviction = pd.DataFrame([
            {
                "Ticker": "TOP1",
                "Conviction Score (0-14)": 5,  # Dropped from 9 to 5 (< 6 threshold)
                "Star Rating": "⭐⭐⭐",
                "Held In (count)": 5,
                "Premium Tags (e.g., Institutional, Deep Value)": "-",
                "Portfolios": "P1_Base",
            }
        ])
        with patch("main_controller.calculate_top_picks_conviction", return_value=mock_dropped_conviction):
            closed = daemon.execute_phase1_exits(p11)
            assert closed == 1
            assert len(p11.positions) == 0
            trades = p11.load_trade_history()
            assert len(trades) == 1
            assert "CONVICTION_DROP" in trades[0]["Exit Reason"]


def test_p11_meta_consensus_blocks_nlp_reject(tmp_path):
    """Verifies that P11 strictly blocks buying a high-conviction candidate if NLP status is REJECT."""
    portfolio_dir = tmp_path / "data" / "portfolios"
    portfolio_dir.mkdir(parents=True)
    p11_cfg = PortfolioConfig(
        portfolio_id="P11_Meta_Consensus",
        strategy="Meta_Consensus",
        slots=5,
        slot_size=2000.0,
        start_cash=10000.0,
        min_conviction_score=8,
        required_tags=["Institutional"],
    )
    p11 = PortfolioInstance(p11_cfg, base_dir=tmp_path)
    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.portfolios = [p11]
    daemon.market_engine = MarketDataEngine(clean_universe_path=tmp_path / "universe.csv")
    daemon.market_engine.fx_rates = {"EURUSD": 1.0, "EURSEK": 10.0}
    daemon.market_engine.universe_metadata = {
        "HOLO": {
            "market": "US",
            "adv_20d_usd": 1000000.0,
            "adv_20d_local": 1000000.0,
            "currency": "USD",
            "current_price": 2.0,
        }
    }
    daemon.market_engine.prices_cache = {"HOLO": 2.0}
    daemon.market_engine.hard_facts_cache = {
        "HOLO": {
            "revenue_growth_yoy_pct": 25.0,
            "cash_runway_months": 24.0,
            "operating_cash_flow_ttm": 100000.0,
            "data_quality": {"is_fresh": True},
        }
    }

    mock_df_conviction = pd.DataFrame([
        {
            "Ticker": "HOLO",
            "Conviction Score (0-14)": 10,
            "Star Rating": "⭐⭐⭐⭐⭐",
            "Held In (count)": 8,
            "Premium Tags (e.g., Institutional, Deep Value)": "💧 Institutional (+1), 💎 Deep Value (+2)",
            "Portfolios": "P1, P4, P7",
        }
    ])

    # Case 1: Global NLP state has REJECT for HOLO -> Must be BLOCKED
    daemon.market_engine.evaluated_news_cache[("HOLO", "Toxic dilution warning")] = (
        "REJECT", "Fatal red flag 'dilution' detected"
    )

    with patch("main_controller.calculate_top_picks_conviction", return_value=mock_df_conviction):
        new_buys = daemon.execute_phase2_screening(p11)
        assert new_buys == 0
        assert len(p11.positions) == 0

    # Case 2: Once REJECT is cleared/replaced with HOLD -> Buy is ALLOWED
    daemon.market_engine.evaluated_news_cache.clear()
    daemon.market_engine.news_radar_cache["HOLO"] = ("HOLD", "Clean press releases")

    with patch("main_controller.calculate_top_picks_conviction", return_value=mock_df_conviction):
        new_buys = daemon.execute_phase2_screening(p11)
        assert new_buys == 1
        assert len(p11.positions) == 1
        assert p11.positions[0]["Ticker"] == "HOLO"


def test_p10_graceful_transition_limit(tmp_path):
    """
    Verify Task 1: P10 gracefully transitions from 20 positions to 5 slots:
    1. Blocks any new BUY orders while open positions (20) >= new target slots (5).
    2. Maintains active SELL logic for all 20 existing positions.
    3. Resumes BUY orders at 2,000 EUR slot size once positions drop below 5.
    """
    cfg = PortfolioConfig(
        portfolio_id="P10_Micro_Sniper",
        strategy="Profile B",
        dead_money_days=180,
        min_adv=150000.0,
        slots=5,
        slot_size=2000.0,
        start_cash=10000.0,
        regions=["FI", "SE", "US"],
    )
    inst = PortfolioInstance(cfg, base_dir=tmp_path)
    inst.cash_balance = 1000.0

    # Populate 20 existing positions
    inst.positions = [
        {
            "Ticker": f"STOCK_{i}.ST",
            "Buy Date": "2026-09-23",
            "Buy Price": 10.0,
            "Shares": 50,
            "Capital Invested": 500.0,
            "Strategy": "Profile B",
            "Currency": "SEK",
        }
        for i in range(20)
    ]
    assert len(inst.positions) == 20

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = MagicMock()
    daemon.market_engine.prices_cache = {f"STOCK_{i}.ST": 10.0 for i in range(20)}
    daemon.market_engine.fx_rates = {"SEK": 11.30, "USD": 1.08}
    daemon.market_engine.news_radar_cache = {}
    daemon.market_engine.hard_facts_cache = {}
    daemon.market_engine.universe_metadata = {}


    # Step 1: execute_phase2_screening must block buys because 20 >= 5
    new_buys = daemon.execute_phase2_screening(inst)
    assert new_buys == 0
    assert len(inst.positions) == 20

    # Step 2: execute_phase1_exits evaluates ALL 20 positions
    # Simulate 16 positions hitting stop-loss / exit
    with patch("main_controller.evaluate_position") as mock_eval:
        def side_eval(position, current_price, latest_news_judgment, latest_financials, dead_money_days):
            idx = int(position["ticker"].split("_")[1].split(".")[0])
            if idx < 16:
                return "SELL", "STOP_LOSS"
            return "HOLD", ""
        mock_eval.side_effect = side_eval

        closed_count = daemon.execute_phase1_exits(inst)
        assert closed_count == 16
        assert len(inst.positions) == 4  # Exactly 4 remaining (< 5)

    # Step 3: Now open_count (4) < cfg.slots (5), BUY is permitted!
    daemon.market_engine.universe_metadata = {
        "NEW_BUY.ST": {
            "current_price": 20.0,
            "market": "SE",
            "adv_20d_local": 300000.0,
            "adv_20d_usd": 30000.0,
            "currency": "SEK",
            "debt_to_equity": 0.2,
            "pe_ratio": 8.0,
        }
    }
    daemon.market_engine.prices_cache["NEW_BUY.ST"] = 20.0
    daemon.market_engine.get_ticker_nlp_status = MagicMock(return_value=("BUY", "Clean"))
    inst.cash_balance = 3000.0

    new_buys_after = daemon.execute_phase2_screening(inst)
    assert new_buys_after == 1
    assert len(inst.positions) == 5
    assert inst.positions[-1]["Ticker"] == "NEW_BUY.ST"


def test_master_daemon_run_scheduler(tmp_path):
    """Test that MasterLiveTradingDaemon.run_scheduler configures daily schedule properly."""
    dummy_yaml = tmp_path / "portfolios.yaml"
    dummy_yaml.write_text("portfolios:\n  P1_Base:\n    strategy: PROFILE_B\n    slots: 5\n    slot_size: 2000\n", encoding="utf-8")

    with patch("main_controller.DEFAULT_PORTFOLIOS_YAML", dummy_yaml), \
         patch("main_controller.PORTFOLIOS_DIR", tmp_path), \
         patch("main_controller.schedule") as mock_schedule:

        daemon = MasterLiveTradingDaemon(config_yaml_path=dummy_yaml)
        daemon.run_cycle = MagicMock(return_value={})

        # Configure mock_schedule
        mock_job = MagicMock()
        mock_schedule.every.return_value.day.at.return_value.do.return_value = mock_job
        # Simulate run_pending raising KeyboardInterrupt to exit loop cleanly
        mock_schedule.run_pending.side_effect = KeyboardInterrupt

        daemon.run_scheduler(daily_at="23:05", run_immediately=True)

        assert daemon.run_cycle.call_count == 1
        mock_schedule.every.return_value.day.at.assert_called_with("23:05")
        mock_schedule.every.return_value.day.at.return_value.do.assert_called_with(daemon.run_cycle)
        # Verify that intraday scanning is not scheduled
        assert daemon.run_intraday_p12_tick not in [call.args[0] for call in mock_schedule.every.return_value.minutes.do.call_args_list]


def test_fetch_live_price_invalidates_stale_momentum_cache():
    engine = MarketDataEngine()
    # Pre-populate momentum_cache with stale data
    engine.momentum_cache["TEST.HE"] = {
        "price": 10.0,
        "day_volume": 1000,
        "day_change_pct": 7.0,
        "runup_3d_pct": 7.0,
        "rsi_14": 65.0,
    }

    mock_ticker = MagicMock()
    mock_fast_info = MagicMock()
    mock_fast_info.last_price = 11.0
    mock_ticker.fast_info = mock_fast_info
    mock_ticker.history.return_value = pd.DataFrame()  # empty history

    with patch("main_controller.yf.Ticker", return_value=mock_ticker):
        price = engine._fetch_live_price("TEST.HE")

    assert price == 11.0
    # Stale momentum cache must have been invalidated/removed
    assert "TEST.HE" not in engine.momentum_cache


def test_fetch_live_price_invalidates_cache_on_exception():
    engine = MarketDataEngine()
    engine.momentum_cache["FAIL.HE"] = {
        "price": 15.0,
        "day_volume": 2000,
        "day_change_pct": 5.0,
        "runup_3d_pct": 5.0,
        "rsi_14": 60.0,
    }

    mock_ticker = MagicMock()
    mock_ticker.history.side_effect = Exception("Network timeout")

    with patch("main_controller.yf.Ticker", return_value=mock_ticker):
        price = engine._fetch_live_price("FAIL.HE")

    assert price is None
    assert "FAIL.HE" not in engine.momentum_cache


def test_nordic_currency_and_fx_conversion():
    from main_controller import get_ticker_currency, get_ticker_region, get_fx_to_account

    # Currencies
    assert get_ticker_currency("MAERSK.CO") == "DKK"
    assert get_ticker_currency("EQUINOR.OL") == "NOK"
    assert get_ticker_currency("NOKIA.HE") == "EUR"
    assert get_ticker_currency("VOLV.ST") == "SEK"
    assert get_ticker_currency("AAPL") == "USD"

    # Regions
    assert get_ticker_region("MAERSK.CO") == "DK"
    assert get_ticker_region("EQUINOR.OL") == "NO"
    assert get_ticker_region("NOKIA.HE") == "FI"
    assert get_ticker_region("VOLV.ST") == "SE"
    assert get_ticker_region("AAPL") == "US"

    # FX conversions
    mock_rates = {"EURUSD": 1.08, "EURSEK": 11.30, "EURDKK": 7.46, "EURNOK": 11.60}
    # 74.60 DKK -> 10.0 EUR
    dkk_to_eur = get_fx_to_account("DKK", "EUR", mock_rates)
    assert round(74.60 * dkk_to_eur, 2) == 10.0

    # 116.0 NOK -> 10.0 EUR
    nok_to_eur = get_fx_to_account("NOK", "EUR", mock_rates)
    assert round(116.0 * nok_to_eur, 2) == 10.0


def test_execute_phase1_exits_forwards_peak_and_catastrophic_stop(tmp_path):
    from main_controller import MasterLiveTradingDaemon, PortfolioConfig, PortfolioInstance

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    cfg = PortfolioConfig(
        portfolio_id="P_TEST",
        strategy="Meta_Consensus",
        slots=1,
        start_cash=10000.0,
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    # Buy price 100, peak was 120, current price drops to 110 (below peak * 0.95 = 114)
    p.positions = [
        {
            "Ticker": "WARN_CO",
            "Buy Price": 100.0,
            "Shares": 10,
            "Capital Invested": 1000.0,
            "highest_price_seen": 120.0,
            "catastrophic_stop": 90.0,
            "Buy Date": "2026-01-01",
        }
    ]

    daemon.market_engine.live_prices = {"WARN_CO": 110.0}
    daemon.market_engine.prices_cache = {"WARN_CO": 110.0}
    daemon.market_engine.news_radar_cache = {"WARN_CO": ("WARN", "Structural warning")}
    daemon.market_engine.hard_facts_cache = {"WARN_CO": {}}
    daemon.market_engine.universe_metadata = {"WARN_CO": {"currency": "USD"}}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("WARN", "Structural warning")
    daemon.market_engine.get_ticker_hard_facts = lambda t: {}

    captured_pos = {}

    def mock_eval(position, current_price, **kwargs):
        captured_pos.update(position)
        return "HOLD", "HOLD"

    with patch("main_controller.evaluate_position", side_effect=mock_eval):
        daemon.execute_phase1_exits(p)

    # Verify evaluate_position received highest_price_seen and catastrophic_stop
    assert captured_pos.get("highest_price_seen") == 120.0
    assert captured_pos.get("catastrophic_stop") == 90.0


def test_p13_ignore_hodl_protection(tmp_path):
    """Positions marked with hodl=True or FARON.HE must never be sold when ignore_hodl_positions=True."""
    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.universe_metadata = {
        "FARON.HE": {"currency": "EUR"},
        "REGULAR.HE": {"currency": "EUR"},
    }
    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        ignore_hodl_positions=True,
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.positions = [
        {
            "Ticker": "FARON.HE",
            "Buy Price": 2.0,
            "Shares": 1000,
            "Capital Invested": 2000.0,
            "Buy Date": "2026-01-01",
            "hodl": True,
        },
        {
            "Ticker": "REGULAR.HE",
            "Buy Price": 10.0,
            "Shares": 100,
            "Capital Invested": 1000.0,
            "Buy Date": "2026-01-01",
            "hodl": False,
        },
    ]

    # Both stocks crash 50%
    daemon.market_engine.prices_cache = {"FARON.HE": 1.0, "REGULAR.HE": 5.0}
    daemon.market_engine.news_radar_cache = {
        "FARON.HE": ("REJECT", "Trial delayed"),
        "REGULAR.HE": ("REJECT", "Dilution"),
    }
    daemon.market_engine.hard_facts_cache = {"FARON.HE": {}, "REGULAR.HE": {}}

    closed = daemon.execute_phase1_exits(p)

    # REGULAR.HE was sold due to -50% loss / REJECT, but FARON.HE was protected and retained!
    assert closed == 1
    assert len(p.positions) == 1
    assert p.positions[0]["Ticker"] == "FARON.HE"


def test_p13_runner_breakeven_rule(tmp_path):
    """Runner rule: after partial take-profit, if price drops below entry buy price, RUNNER_BREAKEVEN_STOP must trigger."""
    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.universe_metadata = {"RUNNER.ST": {"currency": "SEK"}}
    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        partial_take_profit=True,
        partial_trailing_stop_pct=0.15,
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.positions = [
        {
            "Ticker": "RUNNER.ST",
            "Buy Price": 100.0,
            "Peak Price": 130.0,
            "Shares": 50,  # 50 shares remaining after partial TP
            "Capital Invested": 5000.0,
            "Buy Date": "2026-01-01",
            "partial_profit_taken": True,
        }
    ]

    # Case 1: Price drops to 115 (above entry 100, and drop is (130-115)/130 = 11.5% < 15%) -> HOLD
    daemon.market_engine.prices_cache = {"RUNNER.ST": 115.0}
    daemon.market_engine.news_radar_cache = {"RUNNER.ST": ("HOLD", "OK")}
    daemon.market_engine.hard_facts_cache = {"RUNNER.ST": {}}
    closed = daemon.execute_phase1_exits(p)
    assert closed == 0
    assert len(p.positions) == 1

    # Case 2: Price drops to 99 (below entry 100) -> RUNNER_BREAKEVEN_STOP triggers!
    daemon.market_engine.prices_cache = {"RUNNER.ST": 99.0}
    closed = daemon.execute_phase1_exits(p)
    assert closed == 1
    assert len(p.positions) == 0
    trades = p.load_trade_history()
    assert len(trades) == 1
    assert "RUNNER_BREAKEVEN_STOP" in trades[0]["Exit Reason"]


def test_p13_sub_strategies_screening_execution(tmp_path):
    """execute_phase2_sub_strategies_screening enforces slot_sizes and assigns Sub_Strategy metadata."""
    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        start_cash=10000.0,
        sub_strategies={
            "core_nordic_midcap_quality": {
                "allocated_capital": 7000,
                "slots": 3,
                "slot_sizes": [2350, 2350, 2300],
                "universe_filter": "Nordic_MidCap_Compounder",
            },
            "satellite_us_microcap_growth": {
                "allocated_capital": 1500,
                "slots": 1,
                "slot_sizes": [1500],
                "universe_filter": "US_MicroCap_Growth",
            },
            "satellite_nordic_microcap_catalyst": {
                "allocated_capital": 1500,
                "slots": 1,
                "slot_sizes": [1500],
                "universe_filter": "Nordic_MicroCap_Catalyst",
            },
        },
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.cash_balance = 10000.0

    # Mock market engine universe with 1 candidate for each sub-strategy
    daemon.market_engine.universe_metadata = {
        "CORE1.HE": {
            "market": "FI",
            "market_cap_eur": 500_000_000.0,
            "currency": "EUR",
            "adv_20d_local": 200_000.0,
            "current_price": 50.0,
        },
        "USGROW": {
            "market": "US",
            "market_cap_usd": 120_000_000.0,
            "currency": "USD",
            "adv_20d_local": 500_000.0,
            "current_price": 10.0,
            "exchange": "NASDAQ",
        },
        "NORDCAT.ST": {
            "market": "SE",
            "market_cap_eur": 60_000_000.0,
            "currency": "SEK",
            "adv_20d_local": 500_000.0,
            "current_price": 20.0,
        },
    }
    daemon.market_engine.prices_cache = {
        "CORE1.HE": 50.0,
        "USGROW": 10.0,
        "NORDCAT.ST": 20.0,
    }
    daemon.market_engine.hard_facts_cache = {
        "CORE1.HE": {
            "roe": 0.18,
            "ebit_margin": 0.15,
            "free_cash_flow": 20_000_000.0,
            "net_debt_to_ebitda": 1.0,
            "is_profile_b": True,
        },
        "USGROW": {
            "exchange": "NASDAQ",
            "dividend_yield": 0.0,
            "revenue_growth_yoy_pct": 25.0,
            "is_profile_a": True,
        },
        "NORDCAT.ST": {
            "is_profile_b": True,
            "cash_runway_months": "Infinite",
        },
    }
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "OK")

    opened = daemon.execute_phase2_screening(p)

    # 3 positions opened (one for each sub_strategy having valid candidates)
    assert opened == 3
    assert len(p.positions) == 3

    # Check that Sub_Strategy metadata was correctly recorded
    sub_strats_bought = {pos["Sub_Strategy"] for pos in p.positions}
    assert "core_nordic_midcap_quality" in sub_strats_bought
    assert "satellite_us_microcap_growth" in sub_strats_bought
    assert "satellite_nordic_microcap_catalyst" in sub_strats_bought


def test_p13_candidate_ranking_higher_score_beats_alphabetical_order(tmp_path):
    """Higher-scoring candidate alphabetically later (Z) must beat lower-scoring candidate alphabetically earlier (A)."""
    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08}
    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        start_cash=10000.0,
        sub_strategies={
            "satellite_us_microcap_growth": {
                "allocated_capital": 1500,
                "slots": 1,
                "slot_sizes": [1500],
                "universe_filter": "US_MicroCap_Growth",
            },
        },
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.cash_balance = 10000.0

    # A_WEAK comes first alphabetically, but Z_STRONG has much higher score
    daemon.market_engine.universe_metadata = {
        "A_WEAK": {
            "market": "US",
            "market_cap_usd": 100_000_000.0,
            "currency": "USD",
            "adv_20d_local": 1_000_000.0,
            "current_price": 10.0,
            "exchange": "NASDAQ",
        },
        "Z_STRONG": {
            "market": "US",
            "market_cap_usd": 150_000_000.0,
            "currency": "USD",
            "adv_20d_local": 1_500_000.0,
            "current_price": 15.0,
            "exchange": "NASDAQ",
        },
    }
    daemon.market_engine.prices_cache = {"A_WEAK": 10.0, "Z_STRONG": 15.0}
    daemon.market_engine.hard_facts_cache = {
        "A_WEAK": {
            "exchange": "NASDAQ",
            "dividend_yield": 0.0,
            "revenue_growth_yoy_pct": 16.0,  # weak growth
            "is_profile_a": False,
        },
        "Z_STRONG": {
            "exchange": "NASDAQ",
            "dividend_yield": 0.0,
            "revenue_growth_yoy_pct": 60.0,  # explosive growth + profile A
            "is_profile_a": True,
        },
    }
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("STRONG BUY" if t == "Z_STRONG" else "HOLD", "OK")

    opened = daemon.execute_phase2_screening(p)
    assert opened == 1
    assert len(p.positions) == 1
    # Z_STRONG must win the slot over A_WEAK despite alphabetical order!
    assert p.positions[0]["Ticker"] == "Z_STRONG"


def test_p13_rejects_dwarf_position_when_adv_insufficient(tmp_path):
    """Rejects candidate when 5% ADV is less than 80% of the target slot allocation."""
    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08}
    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        start_cash=10000.0,
        sub_strategies={
            "satellite_us_microcap_growth": {
                "allocated_capital": 1500,
                "slots": 1,
                "slot_sizes": [1500],
                "universe_filter": "US_MicroCap_Growth",
            },
        },
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.cash_balance = 10000.0

    # Target slot: 1500 EUR * 1.08 = 1620 USD. 80% is 1296 USD.
    # If adv_20d_local is only 10,000 USD, 5% is only 500 USD (< 1296 USD) -> dwarf position!
    daemon.market_engine.universe_metadata = {
        "DWARF": {
            "market": "US",
            "market_cap_usd": 50_000_000.0,
            "currency": "USD",
            "adv_20d_local": 10_000.0,
            "current_price": 5.0,
            "exchange": "NASDAQ",
        },
    }
    daemon.market_engine.prices_cache = {"DWARF": 5.0}
    daemon.market_engine.hard_facts_cache = {
        "DWARF": {
            "exchange": "NASDAQ",
            "dividend_yield": 0.0,
            "revenue_growth_yoy_pct": 40.0,
            "is_profile_a": True,
        },
    }
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "OK")

    opened = daemon.execute_phase2_screening(p)
    assert opened == 0
    assert len(p.positions) == 0


def test_p13_partial_take_profit_and_runner_rule(tmp_path):
    """P13 triggers partial take-profit (+25% gain -> sells 50% shares) and initiates runner rule."""
    cfg_dict = {
        "description": "Live 10k € OST Hybrid",
        "start_cash": 10000.0,
        "sub_strategies": {
            "core_nordic_midcap_quality": {
                "allocated_capital": 7000,
                "slots": 3,
                "slot_sizes": [2350, 2350, 2300],
                "universe_filter": "Nordic_MidCap_Compounder",
                "rules": [
                    "Partial_TP: Sell 50% at +25.0%",
                    "Runner_Rule: After Partial_TP, move stop to Break-Even (0.0%) and activate -15.0% Trailing_Stop",
                ],
            },
        },
    }
    cfg = PortfolioConfig.from_dict("P13_Live_Candidate", cfg_dict)
    assert cfg.partial_take_profit is True
    assert cfg.take_profit_threshold_pct == 0.25
    assert cfg.partial_sell_fraction == 0.50

    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.cash_balance = 1000.0
    p.positions = [
        {
            "Ticker": "CORE.HE",
            "Shares": 10,
            "Buy Price": 100.0,
            "Buy Date": "2026-01-01",
            "Currency": "EUR",
            "Capital Invested": 1000.0,
            "Sub_Strategy": "core_nordic_midcap_quality",
            "peak_price": 100.0,
        }
    ]

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08}
    daemon.market_engine.prices_cache = {"CORE.HE": 125.0}  # +25% gain
    daemon.market_engine.news_radar_cache = {"CORE.HE": ("HOLD", "OK")}
    daemon.market_engine.hard_facts_cache = {"CORE.HE": {"roe": 0.20, "ebit_margin": 0.15}}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "OK")

    run_trades = []
    daemon.execute_phase1_exits(p, run_trades=run_trades)

    # Partial TP must have occurred: 5 shares sold, 5 remaining
    assert len(p.positions) == 1
    pos = p.positions[0]
    assert pos["Shares"] == 5
    assert pos["partial_profit_taken"] is True
    assert any(t["action"] == "PARTIAL_TP" for t in run_trades)

    # Cash balance increased
    assert p.cash_balance > 1000.0

    # Runner Rule part A: Price drops below entry (100.0) -> Breakeven stop triggers SELL
    daemon.market_engine.prices_cache["CORE.HE"] = 99.0
    run_trades.clear()
    daemon.execute_phase1_exits(p, run_trades=run_trades)
    assert len(p.positions) == 0
    assert any(t["action"] == "SELL" and "RUNNER_BREAKEVEN_STOP" in t["details"]["reason"] for t in run_trades)


def test_p13_satellite_45d_time_stop_and_turnaround_immunity(tmp_path):
    """
    Verifies:
    1. Satellite position held >= 45 trading days with PnL < +5% triggers TIME_STOP_DECAY.
    2. Core/Turnaround positions with negative YoY revenue growth are NOT prematurely sold by generic Profile B Layer 3.
    """
    from datetime import datetime, timezone

    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        slots=5,
        start_cash=10000.0,
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.positions = [
        {
            "Ticker": "STAGNANT.ST",
            "Shares": 100,
            "Buy Price": 10.0,
            "Buy Date": "2026-06-01",  # > 45 trading days ago
            "Currency": "SEK",
            "Capital Invested": 1000.0,
            "Sub_Strategy": "satellite_nordic_microcap_catalyst",
            "Peak Price": 10.2,
        },
        {
            "Ticker": "TURNAROUND.HE",
            "Shares": 100,
            "Buy Price": 10.0,
            "Buy Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),  # Bought today
            "Currency": "EUR",
            "Capital Invested": 1000.0,
            "Sub_Strategy": "satellite_nordic_microcap_catalyst",
            "Peak Price": 10.0,
        },
    ]

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.prices_cache = {
        "STAGNANT.ST": 10.20,     # +2.0% (< +5.0% after >45 trading days -> must exit)
        "TURNAROUND.HE": 10.10,   # +1.0%, fresh buy, negative YoY revenue -> must HOLD
    }
    daemon.market_engine.news_radar_cache = {
        "STAGNANT.ST": ("HOLD", "OK"),
        "TURNAROUND.HE": ("WATCH_TURNAROUND", "Major new order"),
    }
    daemon.market_engine.hard_facts_cache = {
        "STAGNANT.ST": {"revenue_growth_yoy_pct": 10.0, "cash_runway_months": 24.0, "operating_cash_flow_ttm": 100000.0},
        "TURNAROUND.HE": {"revenue_growth_yoy_pct": -15.0, "cash_runway_months": 18.0, "operating_cash_flow_ttm": 50000.0},
    }

    run_trades = []
    closed = daemon.execute_phase1_exits(p, run_trades=run_trades)
    assert closed == 1
    assert len(p.positions) == 1
    assert p.positions[0]["Ticker"] == "TURNAROUND.HE"
    assert any(t["ticker"] == "STAGNANT.ST" and "TIME_STOP_DECAY" in t["details"]["reason"] for t in run_trades)


def test_p13_prevents_low_cash_dwarf_position(tmp_path):
    """Verifies that P13 skips a 2,350 EUR Core slot if available cash is only 400 EUR (< 75% of target slot)."""
    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        slots=5,
        start_cash=10000.0,
        sub_strategies={
            "core_nordic_midcap_quality": {
                "allocated_capital": 7000,
                "slots": 3,
                "slot_sizes": [2350, 2350, 2300],
                "universe_filter": "Nordic_MidCap_Compounder",
            }
        },
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    p.cash_balance = 400.0  # Only 400 EUR left, way below 75% of 2,350 EUR (1,762.50 EUR)
    p.positions = []

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.market_regime = {"regime": "BULL", "last_price": 120.0, "sma_50": 110.0}
    daemon.market_engine.universe_metadata = {
        "HARVIA.HE": {
            "market": "FI",
            "currency": "EUR",
            "market_cap_local": 600_000_000.0,
            "market_cap_usd": 650_000_000.0,
            "adv_20d_local": 500_000.0,
            "adv_20d_usd": 540_000.0,
            "current_price": 40.0,
        }
    }
    daemon.market_engine.prices_cache = {"HARVIA.HE": 40.0}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "Clean")
    daemon.market_engine.get_ticker_hard_facts = lambda t: {
        "roe": 0.25,
        "ebit_margin": 0.20,
        "free_cash_flow": 30_000_000.0,
        "net_debt_to_ebitda": 1.0,
    }

    opened = daemon.execute_phase2_screening(p)
    assert opened == 0
    assert len(p.positions) == 0


def test_nlp_archive_ignores_stale_reject_over_14_days(tmp_path):
    """Verifies that a 60-day-old REJECT in nlp_decisions_archive.csv does not permanently blacklist a ticker."""
    from main_controller import append_nlp_decision

    archive_file = tmp_path / "nlp_decisions_archive.csv"
    universe_file = tmp_path / "clean_microcap_universe.csv"
    universe_file.write_text("ticker,market\nFARON.HE,FI\n", encoding="utf-8")

    # Write a stale REJECT from 2025 (over 14 days old)
    append_nlp_decision(
        ticker="FARON.HE",
        headline="Faron announces share issue",
        llm_decision="REJECT",
        reasoning="Old share issue",
        archive_path=archive_file,
        timestamp="2025-01-10T10:00:00+00:00",
    )

    engine = MarketDataEngine(clean_universe_path=universe_file)
    engine.evaluated_news_cache.clear()
    engine._load_evaluated_news_cache(archive_path=archive_file, max_age_days=14.0)
    engine.news_radar_cache["FARON.HE"] = ("HOLD", "No recent negative news")

    verdict, _ = engine.get_ticker_nlp_status("FARON.HE")
    assert verdict == "HOLD"


def test_set_holding_hodl_does_not_consume_active_slots(tmp_path):
    """Verifies that adding FARON.HE via set_holding(..., hodl=True) preserves all active trading slots."""
    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        start_cash=10000.0,
        slots=1,
        ignore_hodl_positions=True,
        sub_strategies={
            "core_nordic_midcap_quality": {
                "allocated_capital": 2350,
                "slots": 1,
                "slot_sizes": [2350],
                "universe_filter": "Nordic_MidCap_Compounder",
            }
        },
    )
    p = PortfolioInstance(cfg, base_dir=tmp_path)
    pos = p.set_holding("FARON.HE", 2700, 0.40, hodl=True)
    assert pos is not None
    assert pos["Ticker"] == "FARON.HE"
    assert pos["Shares"] == 2700
    assert pos["Buy Price"] == 0.40
    assert pos["hodl"] is True
    assert len(p.positions) == 1
    assert len(p.active_positions()) == 0
    assert p.cash_balance == 10000.0

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.market_regime = {"regime": "BULL", "last_price": 120.0, "sma_50": 110.0}
    daemon.market_engine.universe_metadata = {
        "HARVIA.HE": {
            "market": "FI",
            "currency": "EUR",
            "market_cap_local": 600_000_000.0,
            "market_cap_usd": 650_000_000.0,
            "adv_20d_local": 500_000.0,
            "adv_20d_usd": 540_000.0,
            "current_price": 40.0,
        }
    }
    daemon.market_engine.prices_cache = {"HARVIA.HE": 40.0, "FARON.HE": 0.40}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "Clean")
    daemon.market_engine.get_ticker_hard_facts = lambda t: {
        "roe": 0.25,
        "ebit_margin": 0.20,
        "free_cash_flow": 30_000_000.0,
        "net_debt_to_ebitda": 1.0,
    }

    opened = daemon.execute_phase2_screening(p)
    assert opened == 1
    assert len(p.positions) == 2
    assert len(p.active_positions()) == 1
    assert {x["Ticker"] for x in p.positions} == {"FARON.HE", "HARVIA.HE"}

