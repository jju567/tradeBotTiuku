import json
import logging
from typing import Dict, List, Any
import config

logger = logging.getLogger(__name__)


class StockAdvisorAI:
    """Tiuku AI Agent evaluating equities using technical indicators (RSI, Bollinger Bands, Moving Averages) and GPT-4o / GPT-4o-mini."""

    def __init__(self, api_key: str = config.OPENAI_API_KEY, model: str = config.OPENAI_MODEL):
        self.api_key = api_key
        self.model = model
        self.client = None

        if self.api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key)
                logger.info(f"🤖 OpenAI AIAdvisor initialized successfully (Model: {self.model})")
            except Exception as e:
                logger.warning(f" Could not initialize OpenAI client: {e}. Falling back to rule-based Tiuku engine.")
        else:
            logger.info("ℹ️ OPENAI_API_KEY missing. Using rule-based Tiuku quantitative engine.")

    def evaluate_equities(self, market_data: List[Dict[str, Any]], current_portfolio: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Evaluates equities and assigns an AI Score (1-10), target weight, and reasoning for each stock."""
        results = []
        mode_str = f"OpenAI {self.model}" if self.client else "Rule-based Engine"
        logger.info(f"Starting equity evaluation for {len(market_data)} symbols using {mode_str}...")

        for stock in market_data:
            if self.client:
                score_data = self._call_openai_eval(stock, current_portfolio)
            else:
                score_data = self._rule_based_tiuku_eval(stock)

            results.append({
                "symbol": stock["symbol"],
                "name": stock.get("name", stock["symbol"]),
                "current_price": stock.get("current_price"),
                "rsi_14": stock.get("rsi_14"),
                "bollinger": stock.get("bollinger", {}),
                "trend": stock.get("trend"),
                "dividend_yield": stock.get("dividend_yield"),
                "score": score_data.get("score", 5),
                "recommendation": score_data.get("recommendation", "HOLD"),
                "target_weight": score_data.get("target_weight", 0.05),
                "reasoning": score_data.get("reasoning", "Standard evaluation."),
            })

        return results

    def generate_overall_portfolio_analysis(
        self,
        portfolio_summary: Dict[str, Any],
        ai_evaluations: List[Dict[str, Any]],
        risk_alerts: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Generates a strategic, high-level AI analysis of the overall portfolio health, risk level, and asset allocation."""
        holdings = portfolio_summary.get("holdings", {})
        cash_balance = portfolio_summary.get("cash_balance", 0.0)
        cash_weight = portfolio_summary.get("cash_weight", 0.0) * 100

        # Calculate portfolio metrics
        portfolio_scores = [ai.get("score", 5) for ai in ai_evaluations if ai["symbol"] in holdings]
        avg_score = round(sum(portfolio_scores) / len(portfolio_scores), 1) if portfolio_scores else 6.0

        max_position = max((h.get("weight", 0.0) for h in holdings.values()), default=0.0) * 100
        top_symbol = max(holdings.items(), key=lambda x: x[1].get("weight", 0.0))[0] if holdings else "N/A"
        top_name = holdings.get(top_symbol, {}).get("name", top_symbol)
        hodl_count = sum(1 for h in holdings.values() if h.get("hodl", False))

        if self.client:
            summary_text = self._call_openai_portfolio_summary(portfolio_summary, ai_evaluations, avg_score, max_position, top_name)
        else:
            summary_text = self._rule_based_portfolio_summary(portfolio_summary, avg_score, max_position, top_name, hodl_count, len(risk_alerts))

        health_rating = "ERINOMAINEN" if avg_score >= 7.5 and len(risk_alerts) == 0 else ("HYVÄ" if avg_score >= 6.0 else "KOHTALAINEN")

        return {
            "average_ai_score": avg_score,
            "health_rating": health_rating,
            "top_holding_symbol": top_symbol,
            "top_holding_name": top_name,
            "top_holding_weight_pct": round(max_position, 1),
            "summary_text": summary_text,
        }

    def _call_openai_portfolio_summary(
        self,
        portfolio_summary: Dict[str, Any],
        ai_evaluations: List[Dict[str, Any]],
        avg_score: float,
        max_position_pct: float,
        top_name: str
    ) -> str:
        """Calls OpenAI GPT model for portfolio-level strategic summary."""
        prompt = f"""Olet Tiuku, kokenut salkunhoitaja-AI-agentti. Tee tiivis, 3-4 virkkeen strateginen suomenkielinen yhteenveto salkun yleistilasta:

Salkun kokonaisarvo: {portfolio_summary.get('total_equity', 0.0):,.2f} EUR
Käteisosuus: {portfolio_summary.get('cash_weight', 0.0)*100:.1f}% ({portfolio_summary.get('cash_balance', 0.0):,.2f} EUR)
Omistuksia yhteensä: {len(portfolio_summary.get('holdings', {}))} kpl
Keskimääräinen Tiuku AI -kuntoluku: {avg_score}/10
Suurin yksittäinen painotuskohde: {top_name} ({max_position_pct:.1f}%)

Kirjoita selkeä, ammattimainen ja kannustava suomenkielinen yhteenveto salkun hajautuksesta, riski-asemasta ja tarvittavista tasapainotuksista. Älä käytä JSON-muotoilua, vaan pelkkää tekstiä."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Olet Tiuku, suomenkielinen quant-salkunhoitaja-AI."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"OpenAI overall summary error: {e}")
            return self._rule_based_portfolio_summary(portfolio_summary, avg_score, max_position_pct, top_name, 1, 0)

    def _rule_based_portfolio_summary(
        self,
        portfolio_summary: Dict[str, Any],
        avg_score: float,
        max_position_pct: float,
        top_name: str,
        hodl_count: int,
        alert_count: int
    ) -> str:
        """Generates a rule-based strategic summary in Finnish."""
        cash_pct = portfolio_summary.get("cash_weight", 0.0) * 100
        holdings_count = len(portfolio_summary.get("holdings", {}))

        return (
            f"Salkku on rakenteeltaan tasapainoinen ja kattaa {holdings_count} eri sijoituskohdetta. "
            f"Omistusten keskimääräinen Tiuku AI -kuntoluku on {avg_score}/10. Suurin yksittäinen sijoituskohde on "
            f"{top_name} ({max_position_pct:.1f}% salkusta). Käteisosuus ({cash_pct:.1f}%) tarjoaa "
            f"hyvän liikkumavaran uudelleentasapainotuksille ja houkutteleville lisäostoille."
        )

    def _call_openai_eval(self, stock: Dict[str, Any], current_portfolio: Dict[str, Any]) -> Dict[str, Any]:
        """Calls OpenAI GPT model for Tiuku stock evaluation."""
        bb = stock.get("bollinger", {})
        prompt = f"""You are 'Tiuku', an expert stock market advisor AI for weekly portfolio rebalancing.
Evaluate the following equity based on technical momentum, Bollinger Bands, moving averages, and dividend yield:

Stock Symbol: {stock.get('symbol')} ({stock.get('name')})
Sector: {stock.get('sector')}
Current Price: {stock.get('current_price')} EUR (24h change: {stock.get('change_24h_pct')}%)
RSI (14): {stock.get('rsi_14')}
Bollinger Bands: Middle={bb.get('middle')}, Upper={bb.get('upper')}, Lower={bb.get('lower')}, %B={bb.get('percent_b')}
Trend: {stock.get('trend')} (SMA50: {stock.get('sma_50')}, SMA200: {stock.get('sma_200')}, EMA20: {stock.get('ema_20')})
Dividend Yield: {stock.get('dividend_yield', 0.0)*100:.2f}%
P/E Ratio: {stock.get('pe_ratio')}

TIUKU EVALUATION RULES:
1. Penalize overbought stocks (RSI > 70 or price near/above Upper Bollinger Band %B > 0.9). Recommend trimming/selling.
2. Reward oversold quality stocks (RSI < 38 or price near/below Lower Bollinger Band %B < 0.1). Recommend buying.
3. Favor stocks with stable dividend yield and strong long-term trend (SMA50 > SMA200).
4. Brokerage fees mean low-frequency rebalancing; avoid tiny adjustments. Maximum stock weight limit is {config.MAX_POSITION_WEIGHT*100:.0f}%.

Respond ONLY with valid JSON with keys:
- "score": integer 1 to 10
- "recommendation": "STRONG_BUY", "BUY", "HOLD", "SELL", or "STRONG_SELL"
- "target_weight": float between 0.0 and {config.MAX_POSITION_WEIGHT}
- "reasoning": concise 1-2 sentence explanation
"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are Tiuku, a quantitative stock advisor giving JSON output."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1].strip()
                if content.startswith("json"):
                    content = content[4:].strip()
            return json.loads(content)
        except Exception as e:
            logger.error(f"OpenAI API evaluation error for {stock.get('symbol')}: {e}")
            return self._rule_based_tiuku_eval(stock)

    def _rule_based_tiuku_eval(self, stock: Dict[str, Any]) -> Dict[str, Any]:
        """Rule-based Tiuku engine for scoring stocks (1-10) using RSI, Bollinger Bands, and trend."""
        rsi = stock.get("rsi_14", 50.0)
        div = stock.get("dividend_yield", 0.0)
        trend = stock.get("trend", "NEUTRAL")
        bb = stock.get("bollinger", {})
        pct_b = bb.get("percent_b", 0.5)

        score = 5

        # RSI Evaluation
        if rsi < 35:
            score += 2
        elif rsi > 68:
            score -= 2

        # Bollinger Bands Evaluation (%B)
        if pct_b <= 0.15:
            score += 2  # Attractive dip near lower band
        elif pct_b >= 0.85:
            score -= 2  # Extended near upper band

        # Dividend & Trend
        if div >= 0.04:
            score += 1

        if trend == "BULLISH":
            score += 1
        elif trend == "BEARISH":
            score -= 2

        score = max(1, min(10, score))

        if score >= 8:
            rec = "STRONG_BUY"
            weight = 0.15
        elif score >= 7:
            rec = "BUY"
            weight = 0.10
        elif score <= 3:
            rec = "STRONG_SELL"
            weight = 0.0
        elif score <= 4:
            rec = "SELL"
            weight = 0.02
        else:
            rec = "HOLD"
            weight = 0.05

        reasoning = (
            f"Tiuku score {score}/10 based on RSI ({rsi}), Bollinger %B ({pct_b}), "
            f"Dividend ({div*100:.1f}%), and Trend ({trend})."
        )
        return {"score": score, "recommendation": rec, "target_weight": weight, "reasoning": reasoning}
