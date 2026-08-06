import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any
import config

logger = logging.getLogger(__name__)

# Finnish translation dictionaries for recommendations and trends
REC_TRANSLATIONS = {
    "STRONG_BUY": "VAHVA OSTA",
    "BUY": "OSTA",
    "HOLD": "PIDÄ",
    "SELL": "MYY",
    "STRONG_SELL": "VAHVA MYY",
}

TREND_TRANSLATIONS = {
    "BULLISH": "Nouseva",
    "BEARISH": "Laskeva",
    "NEUTRAL": "Neutraali",
}

ACTION_TRANSLATIONS = {
    "HOLD_LOCKED": "PIDÄ (Lukittu HODL)",
    "SELL_ALL": "MYY KAIKKI (Stop Loss)",
    "REDUCE_POSITION": "PIENENNÄ PAINOA",
}


class WeeklyReporter:
    """Generates Tiuku's weekly portfolio analysis & rebalancing advisor reports (Markdown & HTML Dashboard)."""

    def __init__(self, output_dir: Path = config.REPORT_OUTPUT_DIR):
        self.output_dir = output_dir

    def generate_report(
        self,
        portfolio_summary: Dict[str, Any],
        ai_evaluations: List[Dict[str, Any]],
        proposal: Dict[str, Any],
        risk_alerts: List[Dict[str, Any]],
        overall_ai_summary: Dict[str, Any] = None,
        etf_evaluations: List[Dict[str, Any]] = None
    ) -> Path:
        """Generates both Markdown report and a visual HTML Dashboard in Finnish."""
        if not overall_ai_summary:
            overall_ai_summary = {}
        if etf_evaluations is None:
            etf_evaluations = []

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        report_path = self.output_dir / f"tiuku_weekly_report_{timestamp}.md"

        currency = portfolio_summary.get("currency", "EUR")
        total_equity = portfolio_summary.get("total_equity", 0.0)
        cash_balance = portfolio_summary.get("cash_balance", 0.0)

        lines = [
            "# 🐱 tradeBotTiuku — Viikoittainen Salkunneuvonantoraportti",
            f"**Luotu:** {datetime.now().strftime('%d.%m.%Y klo %H:%M:%S')}  ",
            f"**Tila:** Neuvonanto & Avoin Markkinadata (`yfinance`)  ",
            f"**Salkun Kokonaisarvo:** {total_equity:,.2f} {currency}  ",
            f"**Käteisvarat:** {cash_balance:,.2f} {currency} ({portfolio_summary.get('cash_weight', 0.0)*100:.1f}%)  ",
            "\n---",
            "\n## 🧠 Tiuku AI Strateginen Salkun Yleiskatsaus",
            f"- **Keskimääräinen Tiuku-kuntoluku:** {overall_ai_summary.get('average_ai_score', 6.0)}/10",
            f"- **Salkun Kuntoluokitus:** {overall_ai_summary.get('health_rating', 'HYVÄ')}",
            f"- **Suurin Painotuskohde:** {overall_ai_summary.get('top_holding_name', 'N/A')} ({overall_ai_summary.get('top_holding_weight_pct', 0.0):.1f}%)",
            f"\n> **Tiukun Strateginen Analyysi:**  \n> {overall_ai_summary.get('summary_text', '')}",
            "\n---",
            "\n## 1. ⚠️ Turvarajat & Riskihuomiot",
        ]

        if risk_alerts:
            for alert in risk_alerts:
                rec_action = ACTION_TRANSLATIONS.get(alert['recommended_action'], alert['recommended_action'])
                lines.append(f"- **[{alert['severity']}] {alert['symbol']}**: {alert['message']} -> *Toimenpide: {rec_action}*")
        else:
            lines.append("✅ Ei riskirajarrikkomuksia. Salkku on tasapainossa määriteltyjen turvarajojen puitteissa.")

        lines.extend([
            "\n---",
            "\n## 2. 📊 Salkun Omistukset & Arvostus",
            "| Symboli | Määrä | Keskihinta | Nykykurssi | Markkina-arvo | Nykypaino | Tavoitepaino | Tuotto (%) | Tila |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
        ])

        for sym, h in portfolio_summary.get("holdings", {}).items():
            status_badge = "🔒 HODL (Lottolappu)" if h.get("hodl", False) else "AKTIIVINEN"
            lines.append(
                f"| **{sym}** | {h.get('quantity')} | {h.get('avg_price'):.2f} {currency} | "
                f"{h.get('current_price'):.2f} {currency} | {h.get('market_value'):,.2f} {currency} | "
                f"{h.get('weight')*100:.1f}% | {h.get('target_weight', 0.10)*100:.1f}% | {h.get('unrealized_pnl_pct'):+.1f}% | {status_badge} |"
            )

        lines.extend([
            "\n---",
            "\n## 3. 🤖 Tiuku AI Osakearviot & Tekninen Analyysi",
            "| Symboli | Pisteet | Suositus | Tavoitepaino | RSI(14) | Bollinger %B | Trendi | Perustelu |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
        ])

        portfolio_symbols = set(portfolio_summary.get("holdings", {}).keys())

        for ai in ai_evaluations:
            sym = ai["symbol"]
            score = ai.get("score", 5)
            rec = ai.get("recommendation", "HOLD")
            rec_fi = REC_TRANSLATIONS.get(rec, rec)
            trend_fi = TREND_TRANSLATIONS.get(ai.get("trend", "NEUTRAL").upper(), ai.get("trend", "NEUTRAL"))
            is_in_portfolio = sym in portfolio_symbols
            is_buy = rec in ["BUY", "STRONG_BUY"]

            # Only include if in portfolio OR recommended for BUY
            if not (is_in_portfolio or is_buy):
                continue

            bb = ai.get("bollinger", {})
            pct_b = bb.get("percent_b", "-")
            lines.append(
                f"| **{ai['symbol']}** | **{ai['score']}/10** | `{rec_fi}` | "
                f"{ai.get('target_weight', 0.0)*100:.1f}% | {ai.get('rsi_14')} | {pct_b} | {trend_fi} | {ai['reasoning']} |"
            )

        lines.extend([
            "\n---",
            "\n## 4. 📝 Nordnet / Pankki Manuaalisen Kaupankäynnin Muistilista",
            f"**Ehdotus-ID:** `{proposal.get('proposal_id')}`  ",
            f"**Nordnet-palkkiotaso:** Taso {config.NORDNET_FEE_TIER} (min {config.COMMISSION_MIN_EUR:.2f} {currency} / {config.COMMISSION_PERCENT*100:.2f}%)  ",
            f"**Ehdotettujen kauppojen määrä:** {proposal.get('trade_count', 0)}  ",
            f"**Arvioidut välityspalkkiot yhteensä:** {proposal.get('total_estimated_commission', 0.0):.2f} {currency}  ",
            "\nSuorita seuraavat kaupat manuaalisesti välittäjäsi verkkopalvelussa tai sovelluksessa (esim. Nordnet):",
            "\n| Vaihe | Toimenpide | Symboli | Määrä | Markkinahinta | Arvioitu Kauppa-arvo | Arv. Palkkio | Varmuus / Syy |",
            "| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :--- |",
        ])

        trades = proposal.get("proposed_trades", [])
        if trades:
            for idx, t in enumerate(trades, 1):
                action_fi = "MYY" if t['action'] == "SELL" else "OSTA"
                lines.append(
                    f"| {idx} | **{action_fi}** | **{t['symbol']}** | {t['quantity']} kpl | "
                    f"{t['price']:.2f} {currency} | {t['trade_value']:,.2f} {currency} | "
                    f"{t['estimated_commission']:.2f} {currency} | AI-pisteet: {t.get('ai_score', 'N/A')}/10 - {t['reason']} |"
                )
        else:
            lines.append("| - | - | Ei kauppaehdotuksia tälle syklille. Salkku on optimaalisessa tasapainossa. | - | - | - | - | - |")

        lines.extend([
            "\n---",
            "\n## 5. 🔒 Vastuuvapauslauseke & Tietosuoja",
            "Tämä raportti on luotu paikallisesti tradeBotTiuku-järjestelmällä hyödyntäen avoimen lähdekoodin markkinadataa (`yfinance`).",
            "Kaikki salkkutiedot ja API-avaimet säilyvät 100 % paikallisesti omalla laitteellasi.",
            "*Automaattista kaupankäyntiä ei suoriteta ilman ihmisen vahvistusta.*",
        ])

        report_content = "\n".join(lines)

        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_content)
            logger.info(f"Tiuku weekly report generated successfully: {report_path}")
        except Exception as e:
            logger.error(f"Failed to write report file: {e}")

        # Also generate HTML Dashboard
        self.generate_html_dashboard(portfolio_summary, ai_evaluations, proposal, risk_alerts, overall_ai_summary)

        return report_path

    def generate_html_dashboard(
        self,
        portfolio_summary: Dict[str, Any],
        ai_evaluations: List[Dict[str, Any]],
        proposal: Dict[str, Any],
        risk_alerts: List[Dict[str, Any]],
        overall_ai_summary: Dict[str, Any] = None
    ) -> Path:
        """Generates a visual, interactive HTML dashboard file in Finnish (tiuku_dashboard.html)."""
        if not overall_ai_summary:
            overall_ai_summary = {}

        dashboard_path = config.BASE_DIR / "tiuku_dashboard.html"
        reports_dashboard_path = self.output_dir / "tiuku_dashboard.html"

        currency = portfolio_summary.get("currency", "EUR")
        total_equity = portfolio_summary.get("total_equity", 0.0)
        cash_balance = portfolio_summary.get("cash_balance", 0.0)
        cash_weight = portfolio_summary.get("cash_weight", 0.0) * 100
        holdings = portfolio_summary.get("holdings", {})
        trades = proposal.get("proposed_trades", [])

        # Count active vs HODL positions
        hodl_count = sum(1 for h in holdings.values() if h.get("hodl", False))
        active_count = len(holdings) - hodl_count

        # Build Holdings Rows HTML
        holdings_rows_html = ""
        for sym, h in holdings.items():
            pnl_pct = h.get("unrealized_pnl_pct", 0.0)
            pnl_class = "positive" if pnl_pct >= 0 else "negative"
            pnl_sign = "+" if pnl_pct >= 0 else ""
            status_html = '<span class="badge hodl">🔒 HODL (Lottolappu)</span>' if h.get("hodl", False) else '<span class="badge active">Aktiivinen</span>'
            full_name = h.get("name", sym)

            holdings_rows_html += f"""
            <tr>
                <td title="{full_name}" class="symbol-cell"><strong>{sym}</strong><br><small style="color: var(--text-secondary); font-size: 0.78rem;">{full_name}</small></td>
                <td>{h.get('quantity')}</td>
                <td>{h.get('avg_price'):.2f} {currency}</td>
                <td>{h.get('current_price'):.2f} {currency}</td>
                <td><strong>{h.get('market_value'):,.2f} {currency}</strong></td>
                <td>
                    <div class="weight-bar-wrapper">
                        <span>{h.get('weight')*100:.1f}%</span>
                        <div class="weight-bar" style="width: {min(100, h.get('weight')*500)}%;"></div>
                    </div>
                </td>
                <td>{h.get('target_weight', 0.10)*100:.1f}%</td>
                <td><span class="pnl {pnl_class}">{pnl_sign}{pnl_pct:.1f}%</span></td>
                <td>{status_html}</td>
            </tr>
            """

        # Build AI Scores Rows HTML
        portfolio_symbols = set(holdings.keys())
        ai_rows_html = ""
        for ai in ai_evaluations:
            sym = ai["symbol"]
            score = ai.get("score", 5)
            rec = ai.get("recommendation", "HOLD")
            rec_fi = REC_TRANSLATIONS.get(rec, rec)
            trend_raw = ai.get("trend", "NEUTRAL")
            trend_fi = TREND_TRANSLATIONS.get(trend_raw.upper(), trend_raw)
            full_name = ai.get("name", sym)
            
            is_in_portfolio = sym in portfolio_symbols
            is_buy = rec in ["BUY", "STRONG_BUY"]

            # Filter out non-portfolio stocks that are not recommended for buying
            if not (is_in_portfolio or is_buy):
                continue

            score_color = "#10b981" if score >= 7 else ("#ef4444" if score <= 3 else "#f59e0b")
            rec_class = "buy" if "BUY" in rec else ("sell" if "SELL" in rec else "hold")

            bb = ai.get("bollinger", {})
            pct_b = bb.get("percent_b", 0.5)

            ai_rows_html += f"""
            <tr>
                <td title="{full_name}" class="symbol-cell"><strong>{ai['symbol']}</strong><br><small style="color: var(--text-secondary); font-size: 0.78rem;">{full_name}</small></td>
                <td>
                    <div class="score-container">
                        <span class="score-val" style="color: {score_color};">{score}/10</span>
                        <div class="score-bar-bg"><div class="score-bar-fill" style="width: {score*10}%; background-color: {score_color};"></div></div>
                    </div>
                </td>
                <td><span class="badge rec-{rec_class}">{rec_fi}</span></td>
                <td>{ai.get('target_weight', 0.0)*100:.1f}%</td>
                <td>{ai.get('rsi_14')}</td>
                <td>{pct_b}</td>
                <td><span class="trend-{trend_raw.lower()}">{trend_fi}</span></td>
                <td class="reasoning">{ai['reasoning']}</td>
            </tr>
            """

        # Build Action Checklist Rows HTML
        trades_rows_html = ""
        if trades:
            for idx, t in enumerate(trades, 1):
                action_class = "sell" if t['action'] == "SELL" else "buy"
                action_icon = "🔻 MYY" if t['action'] == "SELL" else "🟢 OSTA"
                full_name = t.get("name", t.get("symbol", ""))
                trades_rows_html += f"""
                <tr>
                    <td><strong>{idx}</strong></td>
                    <td><span class="badge rec-{action_class}">{action_icon}</span></td>
                    <td title="{full_name}" class="symbol-cell"><strong>{t['symbol']}</strong><br><small style="color: var(--text-secondary); font-size: 0.78rem;">{full_name}</small></td>
                    <td>{t['quantity']} kpl</td>
                    <td>{t['price']:.2f} {currency}</td>
                    <td><strong>{t['trade_value']:,.2f} {currency}</strong></td>
                    <td>{t['estimated_commission']:.2f} {currency}</td>
                    <td class="reasoning"><strong>AI {t.get('ai_score', 'N/A')}/10</strong>: {t['reason']}</td>
                </tr>
                """
        else:
            trades_rows_html = "<tr><td colspan='8'>Ei toimenpiteitä tälle syklille. Salkun painotukset ovat ihanteelliset.</td></tr>"

        # Build Risk Alerts HTML
        alerts_html = ""
        if risk_alerts:
            for a in risk_alerts:
                sev_class = a['severity'].lower()
                icon = "🔒" if "HODL" in a['type'] else ("⚠️" if a['severity'] == "HIGH" else "ℹ️")
                rec_action_fi = ACTION_TRANSLATIONS.get(a['recommended_action'], a['recommended_action'])
                full_name = holdings.get(a['symbol'], {}).get('name', a['symbol'])
                alerts_html += f"""
                <div class="alert-card alert-{sev_class}">
                    <div class="alert-header">{icon} <strong title="{full_name}">{a['symbol']}</strong> ({full_name}) - {a['type']}</div>
                    <div class="alert-body">{a['message']}</div>
                    <div class="alert-action">Toimenpideohje: <strong>{rec_action_fi}</strong></div>
                </div>
                """
        else:
            alerts_html = '<div class="alert-card alert-success">✅ Ei riskirajarrikkomuksia. Salkku on tasapainossa.</div>'

        # Build ETF Watchlist Rows HTML
        etf_rows_html = ""
        if etf_evaluations:
            for etf in etf_evaluations:
                score = etf.get("score", 5.0)
                score_color = "#34d399" if score >= 8.0 else ("#38bdf8" if score >= 6.5 else "#94a3b8")
                rec_text = etf.get("recommendation", "ODOTA")
                reasons_text = "; ".join(etf.get("reasons", [])) if etf.get("reasons") else etf.get("notes", "")
                full_name = etf.get("name", etf.get("symbol", ""))
                ter = etf.get("ter_percent", 0.0)
                
                etf_rows_html += f"""
                <tr>
                    <td title="{full_name}" class="symbol-cell"><strong>{etf['symbol']}</strong></td>
                    <td><small style="color: var(--text-primary); font-weight: 500;">{full_name}</small></td>
                    <td><span class="badge active">{etf.get('category', 'ETF').upper()}</span></td>
                    <td>
                        <div class="score-container">
                            <span class="score-val" style="color: {score_color};">{score}/10</span>
                            <div class="score-bar-bg"><div class="score-bar-fill" style="width: {score*10}%; background-color: {score_color};"></div></div>
                        </div>
                    </td>
                    <td><span class="badge" style="background: rgba(56, 189, 248, 0.15); color: {score_color}; border: 1px solid {score_color};">{rec_text}</span></td>
                    <td>{etf.get('rsi_14', 50.0):.1f}</td>
                    <td>{etf.get('sma_50', 0.0):.2f} {currency}</td>
                    <td>{ter:.2f}%</td>
                    <td class="reasoning">{reasons_text}</td>
                </tr>
                """
        else:
            etf_rows_html = "<tr><td colspan='9'>ETF-seurantalista ei saatavilla.</td></tr>"

        html_content = f"""<!DOCTYPE html>
<html lang="fi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🐱 tradeBotTiuku Dashboard</title>
    <link rel="icon" type="image/svg+xml" href="tiuku.svg">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --border-color: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-cyan: #38bdf8;
            --accent-emerald: #10b981;
            --accent-amber: #f59e0b;
            --accent-rose: #f43f5e;
            --accent-purple: #a855f7;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            line-height: 1.6;
            padding: 24px;
        }}
        .container {{ max-width: 1300px; margin: 0 auto; }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 24px;
        }}
        .logo-section h1 {{ font-size: 1.8rem; font-weight: 700; color: var(--accent-cyan); display: flex; align-items: center; gap: 8px; }}
        .logo-section p {{ font-size: 0.9rem; color: var(--text-secondary); }}
        .header-meta {{ text-align: right; }}
        .badge-pill {{ display: inline-block; padding: 4px 12px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; background: #334155; color: #cbd5e1; }}
        .badge-pill.live {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }}
        
        /* KPI Cards Grid */
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }}
        .kpi-card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }}
        .kpi-card:hover {{ transform: translateY(-2px); box-shadow: 0 10px 20px rgba(0,0,0,0.3); }}
        .kpi-title {{ font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-secondary); font-weight: 600; }}
        .kpi-value {{ font-size: 1.7rem; font-weight: 700; margin: 8px 0 4px; color: #ffffff; }}
        .kpi-sub {{ font-size: 0.8rem; color: var(--text-secondary); }}
        
        /* Section Containers */
        .section-card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 24px;
            margin-bottom: 28px;
        }}
        .section-header {{ font-size: 1.25rem; font-weight: 600; margin-bottom: 16px; color: var(--accent-cyan); display: flex; align-items: center; gap: 8px; }}
        
        /* Alerts */
        .alerts-wrapper {{ display: flex; flex-direction: column; gap: 12px; }}
        .alert-card {{ padding: 14px 18px; border-radius: 8px; border-left: 4px solid #3b82f6; background: rgba(30, 41, 59, 0.8); }}
        .alert-high {{ border-left-color: var(--accent-rose); background: rgba(244, 63, 94, 0.1); }}
        .alert-medium {{ border-left-color: var(--accent-amber); background: rgba(245, 158, 11, 0.1); }}
        .alert-info {{ border-left-color: var(--accent-cyan); background: rgba(56, 189, 248, 0.1); }}
        .alert-success {{ border-left-color: var(--accent-emerald); background: rgba(16, 185, 129, 0.1); }}
        .alert-header {{ font-weight: 600; font-size: 0.95rem; margin-bottom: 4px; }}
        .alert-body {{ font-size: 0.85rem; color: #cbd5e1; }}
        .alert-action {{ font-size: 0.8rem; color: var(--accent-cyan); margin-top: 4px; }}
        
        /* Tables */
        table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; text-align: left; }}
        th {{ background: #0f172a; padding: 12px 14px; color: var(--text-secondary); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; border-bottom: 1px solid var(--border-color); }}
        td {{ padding: 12px 14px; border-bottom: 1px solid var(--border-color); vertical-align: middle; }}
        tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}
        
        /* Badges & Formatters */
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }}
        .badge.active {{ background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }}
        .badge.hodl {{ background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4); }}
        .badge.rec-buy {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }}
        .badge.rec-sell {{ background: rgba(244, 63, 94, 0.2); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }}
        .badge.rec-hold {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }}
        
        .pnl.positive {{ color: #34d399; font-weight: 600; }}
        .pnl.negative {{ color: #fb7185; font-weight: 600; }}
        
        .score-container {{ display: flex; align-items: center; gap: 8px; width: 100px; }}
        .score-val {{ font-weight: 700; font-size: 0.9rem; min-width: 32px; }}
        .score-bar-bg {{ flex: 1; height: 6px; background: #334155; border-radius: 3px; overflow: hidden; }}
        .score-bar-fill {{ height: 100%; border-radius: 3px; }}
        
        .weight-bar-wrapper {{ display: flex; align-items: center; gap: 8px; width: 90px; }}
        .weight-bar {{ height: 6px; background: var(--accent-cyan); border-radius: 3px; }}
        
        .reasoning {{ font-size: 0.82rem; color: #cbd5e1; max-width: 350px; line-height: 1.4; }}
        .trend-bullish {{ color: #34d399; font-weight: 600; }}
        .trend-bearish {{ color: #fb7185; font-weight: 600; }}
        .trend-neutral {{ color: #94a3b8; }}
        
        footer {{ text-align: center; font-size: 0.8rem; color: var(--text-secondary); margin-top: 30px; padding-top: 20px; border-top: 1px solid var(--border-color); }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-section" style="display: flex; align-items: center; gap: 14px;">
                <img src="tiuku.svg" alt="tradeBotTiuku Logo" style="width: 54px; height: 54px; border-radius: 12px; filter: drop-shadow(0 4px 10px rgba(56,189,248,0.35));">
                <div>
                    <h1>tradeBotTiuku Dashboard</h1>
                    <p>Avointen Lähteiden Salkunneuvonantaja & AI-Agentti (Päivitetty: {datetime.now().strftime('%d.%m.%Y klo %H:%M')})</p>
                </div>
            </div>
            <div class="header-meta">
                <span class="badge-pill live">● Avoin Markkinadata (yfinance)</span>
                <p style="font-size: 0.8rem; color: var(--text-secondary); margin-top: 4px;">Ihmisen valvoma (Human-in-the-loop) • Vain neuvonanto</p>
            </div>
        </header>

        <!-- KPI Grid -->
        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-title">Salkun Kokonaisarvo</div>
                <div class="kpi-value">{total_equity:,.2f} {currency}</div>
                <div class="kpi-sub">Käteinen: {cash_balance:,.2f} {currency} ({cash_weight:.1f}%)</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Seurattavat Omistukset</div>
                <div class="kpi-value">{len(holdings)} kpl</div>
                <div class="kpi-sub">{active_count} Aktiivista • {hodl_count} HODL-suojattua</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Toimenpide-ehdotukset</div>
                <div class="kpi-value">{proposal.get('trade_count', 0)} kauppaa</div>
                <div class="kpi-sub">Välityspalkkiot arvio: {proposal.get('total_estimated_commission', 0.0):.2f} {currency}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">Riski- & HODL-tila</div>
                <div class="kpi-value" style="color: {'#38bdf8' if not risk_alerts else '#f59e0b'};">{len(risk_alerts)} Huomiota</div>
                <div class="kpi-sub">Faron HODL-lukitus aktiivinen</div>
            </div>
        </div>

        <!-- Overall AI Portfolio Strategic Assessment -->
        <div class="section-card" style="border-left: 4px solid var(--accent-purple); background: linear-gradient(135deg, rgba(30, 41, 59, 0.95), rgba(15, 23, 42, 0.95));">
            <div class="section-header" style="color: #c084fc;">🧠 Tiuku AI Strateginen Salkun Yleiskatsaus & Terveystarkastus</div>
            <div style="display: flex; flex-wrap: wrap; gap: 16px; align-items: center; margin-bottom: 16px;">
                <div style="background: rgba(168, 85, 247, 0.15); border: 1px solid rgba(168, 85, 247, 0.4); padding: 10px 18px; border-radius: 10px; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600;">Keskimääräinen Tiuku Score:</span>
                    <span style="font-size: 1.25rem; font-weight: 700; color: #c084fc;">{overall_ai_summary.get('average_ai_score', 6.0)} / 10</span>
                </div>
                <div style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.4); padding: 10px 18px; border-radius: 10px; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600;">Suurin Yksittäinen Paino:</span>
                    <span style="font-size: 1.05rem; font-weight: 700; color: #38bdf8;">{overall_ai_summary.get('top_holding_name', 'N/A')} ({overall_ai_summary.get('top_holding_weight_pct', 0.0):.1f}%)</span>
                </div>
                <div style="background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); padding: 10px 18px; border-radius: 10px; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600;">Salkun Kuntoluokitus:</span>
                    <span style="font-size: 1.05rem; font-weight: 700; color: #34d399;">{overall_ai_summary.get('health_rating', 'HYVÄ')}</span>
                </div>
            </div>
            <p style="font-size: 0.92rem; color: #e2e8f0; line-height: 1.7; background: rgba(15, 23, 42, 0.6); padding: 16px; border-radius: 10px; border: 1px solid var(--border-color);">
                💬 <strong>Tiuku AI:n Strateginen Analyysi:</strong><br>
                {overall_ai_summary.get('summary_text', '')}
            </p>
        </div>

        <!-- Risk Alerts Section -->
        <div class="section-card">
            <div class="section-header">⚠️ Turvarajat, Riskihuomiot & HODL-suojat</div>
            <div class="alerts-wrapper">
                {alerts_html}
            </div>
        </div>

        <!-- Manual Execution Checklist Section -->
        <div class="section-card">
            <div class="section-header">📝 Nordnet / Pankki Manuaalisen Kaupankäynnin Muistilista</div>
            <p style="font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 16px;">
                Voit suorittaa seuraavat ehdotetut kaupat manuaalisesti verkkopankissasi tai Nordnet-sovelluksessa. Kaupat on valikoitu kulutehokkaasti (minimi kauppakoko 200 €).
            </p>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Toimenpide</th>
                            <th>Symboli</th>
                            <th>Määrä</th>
                            <th>Kurssi</th>
                            <th>Arvioitu Arvo</th>
                            <th>Palkkio</th>
                            <th>Syy & Tiuku AI Pisteytys</th>
                        </tr>
                    </thead>
                    <tbody>
                        {trades_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Holdings Breakdown Section -->
        <div class="section-card">
            <div class="section-header">📊 Salkun Omistukset & Arvostustilanne</div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>Symboli</th>
                            <th>Määrä</th>
                            <th>Keskihinta</th>
                            <th>Nykykurssi</th>
                            <th>Markkina-arvo</th>
                            <th>Nykypaino</th>
                            <th>Tavoitepaino</th>
                            <th>Tuotto (PnL)</th>
                            <th>Tila</th>
                        </tr>
                    </thead>
                    <tbody>
                        {holdings_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ETF Watchlist & Buy the Dip Section -->
        <div class="section-card" style="border-left: 4px solid var(--accent-cyan);">
            <div class="section-header">📈 ETF-Seurantalista & Kuukausisäästön Dippianalyysi (Buy the Dip)</div>
            <p style="font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 16px;">
                Seurattavat globaalit indeksirahastot luokiteltuna teemoittain. Tiuku tunnistaa ostopaikat (RSI ≤ 40, hinta &lt; SMA50) kuukausisäästön kohdentamiseen.
            </p>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>Symboli</th>
                            <th>Nimi</th>
                            <th>Kategoria</th>
                            <th>Dippipisteet</th>
                            <th>Suositus</th>
                            <th>RSI(14)</th>
                            <th>SMA(50)</th>
                            <th>TER %</th>
                            <th>Huomiot & Syy</th>
                        </tr>
                    </thead>
                    <tbody>
                        {etf_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- AI Ratings Section -->
        <div class="section-card">
            <div class="section-header">🤖 Tiuku AI Osakearviot & Tekninen Analyysi (GPT-4o)</div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>Osake / ETF</th>
                            <th>Tiuku-pisteet</th>
                            <th>Suositus</th>
                            <th>Tavoitepaino</th>
                            <th>RSI(14)</th>
                            <th>Bollinger %B</th>
                            <th>Trendi</th>
                            <th>Tiukun Analyysi & Perustelu</th>
                        </tr>
                    </thead>
                    <tbody>
                        {ai_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <footer>
            🐱 tradeBotTiuku — Avointen Lähteiden Salkunneuvonantaja & AI-Agentti <br>
            Kaikki salkkutiedot ja API-avaimet säilyvät aina 100% paikallisesti omalla laitteellasi.
        </footer>
    </div>
</body>
</html>
"""

        try:
            with open(dashboard_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            with open(reports_dashboard_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            # Ensure tiuku.svg is copied to reports folder
            svg_source = config.BASE_DIR / "tiuku.svg"
            svg_target = self.output_dir / "tiuku.svg"
            if svg_source.exists():
                with open(svg_source, "r", encoding="utf-8") as sf:
                    svg_data = sf.read()
                with open(svg_target, "w", encoding="utf-8") as tf:
                    tf.write(svg_data)

            logger.info(f"Generated HTML Dashboard at: {dashboard_path} and {reports_dashboard_path}")
        except Exception as e:
            logger.error(f"Failed to write HTML dashboard: {e}")

        return dashboard_path
