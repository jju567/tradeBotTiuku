"""Unit tests for core/ai_advisor.py — StockAdvisorAI (rule-based engine, no OpenAI API calls)."""
import pytest
from core.ai_advisor import StockAdvisorAI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_stock(
    symbol: str = "TEST.HE",
    rsi_14: float = 50.0,
    percent_b: float = 0.5,
    trend: str = "NEUTRAL",
    dividend_yield: float = 0.0,
    current_price: float = 10.0,
    change_24h_pct: float = 0.0,
    sector: str = "General",
    sma_50: float = 10.0,
    sma_200: float = 10.0,
    ema_20: float = 10.0,
) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "sector": sector,
        "current_price": current_price,
        "change_24h_pct": change_24h_pct,
        "rsi_14": rsi_14,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "ema_20": ema_20,
        "bollinger": {
            "middle": current_price,
            "upper": current_price * 1.1,
            "lower": current_price * 0.9,
            "percent_b": percent_b,
        },
        "trend": trend,
        "dividend_yield": dividend_yield,
        "pe_ratio": 15.0,
    }


@pytest.fixture
def rule_based_advisor() -> StockAdvisorAI:
    """StockAdvisorAI without OpenAI key — always uses rule-based engine."""
    return StockAdvisorAI(api_key="", model="gpt-4o")


# ---------------------------------------------------------------------------
# _rule_based_tiuku_eval
# ---------------------------------------------------------------------------


class TestRuleBasedTiukuEval:
    def test_returns_dict_with_required_keys(self, rule_based_advisor):
        result = rule_based_advisor._rule_based_tiuku_eval(make_stock())
        assert "score" in result
        assert "recommendation" in result
        assert "target_weight" in result
        assert "reasoning" in result

    def test_score_is_in_valid_range(self, rule_based_advisor):
        for rsi in [20, 35, 50, 65, 80]:
            stock = make_stock(rsi_14=float(rsi))
            result = rule_based_advisor._rule_based_tiuku_eval(stock)
            assert 1 <= result["score"] <= 10

    def test_oversold_rsi_increases_score(self, rule_based_advisor):
        oversold = rule_based_advisor._rule_based_tiuku_eval(make_stock(rsi_14=25.0))
        neutral = rule_based_advisor._rule_based_tiuku_eval(make_stock(rsi_14=50.0))
        assert oversold["score"] > neutral["score"]

    def test_overbought_rsi_decreases_score(self, rule_based_advisor):
        overbought = rule_based_advisor._rule_based_tiuku_eval(make_stock(rsi_14=75.0))
        neutral = rule_based_advisor._rule_based_tiuku_eval(make_stock(rsi_14=50.0))
        assert overbought["score"] < neutral["score"]

    def test_low_percent_b_increases_score(self, rule_based_advisor):
        near_lower = rule_based_advisor._rule_based_tiuku_eval(make_stock(percent_b=0.10))
        middle = rule_based_advisor._rule_based_tiuku_eval(make_stock(percent_b=0.50))
        assert near_lower["score"] > middle["score"]

    def test_high_percent_b_decreases_score(self, rule_based_advisor):
        near_upper = rule_based_advisor._rule_based_tiuku_eval(make_stock(percent_b=0.90))
        middle = rule_based_advisor._rule_based_tiuku_eval(make_stock(percent_b=0.50))
        assert near_upper["score"] < middle["score"]

    def test_bullish_trend_increases_score(self, rule_based_advisor):
        bullish = rule_based_advisor._rule_based_tiuku_eval(make_stock(trend="BULLISH"))
        neutral = rule_based_advisor._rule_based_tiuku_eval(make_stock(trend="NEUTRAL"))
        assert bullish["score"] > neutral["score"]

    def test_bearish_trend_decreases_score(self, rule_based_advisor):
        bearish = rule_based_advisor._rule_based_tiuku_eval(make_stock(trend="BEARISH"))
        neutral = rule_based_advisor._rule_based_tiuku_eval(make_stock(trend="NEUTRAL"))
        assert bearish["score"] < neutral["score"]

    def test_high_dividend_increases_score(self, rule_based_advisor):
        high_div = rule_based_advisor._rule_based_tiuku_eval(make_stock(dividend_yield=0.06))
        no_div = rule_based_advisor._rule_based_tiuku_eval(make_stock(dividend_yield=0.0))
        assert high_div["score"] > no_div["score"]

    def test_strong_buy_recommendation_for_high_score(self, rule_based_advisor):
        # RSI very low + near lower band + bullish + dividend
        stock = make_stock(rsi_14=20.0, percent_b=0.05, trend="BULLISH", dividend_yield=0.05)
        result = rule_based_advisor._rule_based_tiuku_eval(stock)
        assert result["recommendation"] in ("STRONG_BUY", "BUY")
        assert result["score"] >= 7

    def test_strong_sell_recommendation_for_low_score(self, rule_based_advisor):
        # RSI very high + near upper band + bearish
        stock = make_stock(rsi_14=80.0, percent_b=0.95, trend="BEARISH")
        result = rule_based_advisor._rule_based_tiuku_eval(stock)
        assert result["recommendation"] in ("STRONG_SELL", "SELL")
        assert result["score"] <= 4

    def test_reasoning_string_is_not_empty(self, rule_based_advisor):
        result = rule_based_advisor._rule_based_tiuku_eval(make_stock())
        assert isinstance(result["reasoning"], str)
        assert len(result["reasoning"]) > 0

    def test_target_weight_is_zero_for_strong_sell(self, rule_based_advisor):
        stock = make_stock(rsi_14=80.0, percent_b=0.95, trend="BEARISH")
        result = rule_based_advisor._rule_based_tiuku_eval(stock)
        if result["recommendation"] == "STRONG_SELL":
            assert result["target_weight"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# evaluate_equities — integration (rule-based path)
# ---------------------------------------------------------------------------


class TestEvaluateEquities:
    def test_returns_list_of_same_length_as_input(self, rule_based_advisor):
        stocks = [make_stock("A.HE"), make_stock("B.HE"), make_stock("C.HE")]
        results = rule_based_advisor.evaluate_equities(stocks, current_portfolio={})
        assert len(results) == 3

    def test_each_result_has_symbol(self, rule_based_advisor):
        stocks = [make_stock("NOKIA.HE")]
        results = rule_based_advisor.evaluate_equities(stocks, current_portfolio={})
        assert results[0]["symbol"] == "NOKIA.HE"

    def test_each_result_has_score_in_range(self, rule_based_advisor):
        stocks = [make_stock("TEST.HE", rsi_14=60.0)]
        results = rule_based_advisor.evaluate_equities(stocks, current_portfolio={})
        assert 1 <= results[0]["score"] <= 10

    def test_empty_input_returns_empty_list(self, rule_based_advisor):
        results = rule_based_advisor.evaluate_equities([], current_portfolio={})
        assert results == []


# ---------------------------------------------------------------------------
# generate_overall_portfolio_analysis — rule-based path
# ---------------------------------------------------------------------------


class TestGenerateOverallPortfolioAnalysis:
    def test_returns_dict_with_required_keys(self, rule_based_advisor):
        portfolio = {
            "total_equity": 10000.0,
            "cash_balance": 1000.0,
            "cash_weight": 0.10,
            "holdings": {},
        }
        ai_evals = [{"symbol": "A.HE", "score": 7}]
        result = rule_based_advisor.generate_overall_portfolio_analysis(portfolio, ai_evals, [])
        assert "average_ai_score" in result
        assert "health_rating" in result
        assert "summary_text" in result

    def test_health_rating_excellent_for_high_score_no_alerts(self, rule_based_advisor):
        portfolio = {
            "total_equity": 10000.0,
            "cash_balance": 500.0,
            "cash_weight": 0.05,
            "holdings": {"A.HE": {"weight": 0.10, "hodl": False, "name": "Alpha Corp"}},
        }
        ai_evals = [{"symbol": "A.HE", "score": 9}]
        result = rule_based_advisor.generate_overall_portfolio_analysis(portfolio, ai_evals, [])
        assert result["health_rating"] == "ERINOMAINEN"

    def test_health_rating_degrades_for_low_score(self, rule_based_advisor):
        portfolio = {
            "total_equity": 10000.0,
            "cash_balance": 500.0,
            "cash_weight": 0.05,
            "holdings": {"A.HE": {"weight": 0.10, "hodl": False, "name": "Alpha Corp"}},
        }
        ai_evals = [{"symbol": "A.HE", "score": 4}]
        result = rule_based_advisor.generate_overall_portfolio_analysis(portfolio, ai_evals, [])
        assert result["health_rating"] in ("HYVÄ", "KOHTALAINEN")
