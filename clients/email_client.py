import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import config

logger = logging.getLogger(__name__)


class EmailClient:
    """Sends weekly portfolio reports and visual HTML dashboards via SMTP email."""

    def __init__(
        self,
        smtp_server: str = config.SMTP_SERVER,
        smtp_port: int = config.SMTP_PORT,
        username: str = config.SMTP_USERNAME,
        password: str = config.SMTP_PASSWORD,
        email_from: str = config.EMAIL_FROM,
        email_to: str = config.EMAIL_TO,
    ):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.email_from = email_from or username
        self.email_to = email_to

    def is_configured(self) -> bool:
        """Returns True if minimum required SMTP parameters are configured."""
        return bool(self.smtp_server and self.email_to)

    def send_report_email(
        self,
        portfolio_summary: Dict[str, Any],
        proposal: Dict[str, Any],
        risk_alerts: list,
        overall_ai_summary: Dict[str, Any],
        dashboard_path: Path,
        report_md_path: Optional[Path] = None,
    ) -> bool:
        """Constructs and sends the weekly portfolio email with HTML body and dashboard attachment."""
        if not self.is_configured():
            logger.warning("SMTP email configuration incomplete (check SMTP_SERVER, EMAIL_TO). Skipping email sending.")
            return False

        currency = portfolio_summary.get("currency", "EUR")
        total_equity = portfolio_summary.get("total_equity", 0.0)
        cash_balance = portfolio_summary.get("cash_balance", 0.0)
        cash_weight = portfolio_summary.get("cash_weight", 0.0) * 100
        trade_count = proposal.get("trade_count", 0)
        est_commission = proposal.get("total_estimated_commission", 0.0)
        date_str = datetime.now().strftime("%d.%m.%Y")

        subject = f"🐱 tradeBotTiuku — Viikoittainen Salkkuraportti {date_str} (Kokonaisarvo: {total_equity:,.2f} {currency})"

        # Create MIME message
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = self.email_from or self.username or "tiuku@local"
        msg["To"] = self.email_to

        # Build HTML Email Body
        trades_list_html = ""
        proposed_trades = proposal.get("proposed_trades", [])
        if proposed_trades:
            for idx, t in enumerate(proposed_trades, 1):
                action_icon = "🔻 MYY" if t['action'] == "SELL" else "🟢 OSTA"
                trades_list_html += f"""
                <li style="margin-bottom: 8px;">
                    <strong>{idx}. {action_icon} {t['symbol']}</strong> ({t.get('name', '')}): 
                    {t['quantity']} kpl @ {t['price']:.2f} {currency} (yht. {t['trade_value']:,.2f} {currency})
                    <br><small style="color: #64748b;">AI-pisteet {t.get('ai_score', 'N/A')}/10 — {t['reason']}</small>
                </li>
                """
        else:
            trades_list_html = "<li><em>Ei kauppaehdotuksia tälle syklille. Salkku on optimaalisessa tasapainossa.</em></li>"

        ACTION_TRANSLATIONS = {
            "SELL_ALL": "MYY KAIKKI",
            "TAKE_PROFIT_TRIM": "MYY OSITTAIN (TAKE-PROFIT)",
            "REDUCE_POSITION": "PIENENNÄ PAINOA",
            "HOLD_LOCKED": "PIDÄ LUKITTUNA (HODL)",
            "BUY_MORE": "LISÄÄ OSTOJA",
        }
        alerts_list_html = ""
        if risk_alerts:
            holdings = portfolio_summary.get("holdings", {})
            for a in risk_alerts:
                sym = a.get("symbol", "")
                name = a.get("name") or holdings.get(sym, {}).get("name") or sym
                disp = a.get("display_name") or (f"{name} ({sym})" if name and name != sym else sym)
                rec_fi = ACTION_TRANSLATIONS.get(a.get("recommended_action"), a.get("recommended_action"))
                alerts_list_html += f"<li>⚠️ <strong>{disp}</strong>: {a['message']} (Toimenpide: {rec_fi})</li>"
        else:
            alerts_list_html = "<li>✅ Ei riskirajarrikkomuksia. Salkku on tasapainossa.</li>"

        body_html = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; max-width: 650px; margin: 0 auto; padding: 20px;">
            <div style="background: #0f172a; color: #f8fafc; padding: 20px; border-radius: 10px; margin-bottom: 20px;">
                <h2 style="color: #38bdf8; margin: 0 0 10px 0;">🐱 tradeBotTiuku — Viikkoanalyysi</h2>
                <p style="margin: 0; font-size: 0.9rem; color: #94a3b8;">Ajo suoritettu {datetime.now().strftime('%d.%m.%Y klo %H:%M')}</p>
            </div>

            <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 18px; border-radius: 8px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; color: #0f172a;">📊 Salkun Yhteenveto</h3>
                <ul style="padding-left: 20px; margin: 0;">
                    <li><strong>Salkun Kokonaisarvo:</strong> {total_equity:,.2f} {currency}</li>
                    <li><strong>Käteisvarat:</strong> {cash_balance:,.2f} {currency} ({cash_weight:.1f}%)</li>
                    <li><strong>Seurattavat Omistukset:</strong> {len(portfolio_summary.get('holdings', {}))} kpl</li>
                    <li><strong>Ehdotetut Kaupat:</strong> {trade_count} kpl (Arv. Palkkiot: {est_commission:.2f} {currency})</li>
                </ul>
            </div>

            <div style="background: #f0fdf4; border-left: 4px solid #10b981; padding: 16px; border-radius: 6px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; color: #065f46;">🧠 Tiuku AI Strateginen Analyysi</h3>
                <p style="margin-bottom: 8px;"><strong>Kuntoluokitus:</strong> {overall_ai_summary.get('health_rating', 'HYVÄ')} (Keskiarvo {overall_ai_summary.get('average_ai_score', 6.0)}/10)</p>
                <p style="margin: 0; font-size: 0.95rem; color: #15803d;">{overall_ai_summary.get('summary_text', '')}</p>
            </div>

            <div style="background: #fff; border: 1px solid #e2e8f0; padding: 18px; border-radius: 8px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; color: #0f172a;">📝 Ehdotetut Toimenpiteet (Checklist)</h3>
                <p style="font-size: 0.85rem; color: #64748b; margin-top: -5px; margin-bottom: 12px;">
                    <em>Käytettävissä oleva ostokäteinen: <strong>{proposal.get('available_buy_cash', cash_balance):,.2f} {currency}</strong>. Kaikki ostoehdotukset on sovitettu käteisvaroihisi — voit toteuttaa tämän kokonaisuuden sellaisenaan.</em>
                </p>
                <ol style="padding-left: 20px; margin: 0;">
                    {trades_list_html}
                </ol>
            </div>

            <div style="background: #fff; border: 1px solid #e2e8f0; padding: 18px; border-radius: 8px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; color: #0f172a;">⚠️ Turvarajat & Huomautukset</h3>
                <ul style="padding-left: 20px; margin: 0;">
                    {alerts_list_html}
                </ul>
            </div>

            <p style="font-size: 0.85rem; color: #64748b; margin-top: 25px; border-top: 1px solid #e2e8f0; padding-top: 15px;">
                📎 <em>Interaktiivinen HTML Dashboard (tiuku_dashboard.html) on liitetty tämän sähköpostin liitteeksi. Voit avata sen selaimessasi tarkan näkymän tarkasteluun.</em>
            </p>
        </body>
        </html>
        """

        msg.attach(MIMEText(body_html, "html", "utf-8"))

        # Attach tiuku_dashboard.html
        if dashboard_path and dashboard_path.exists():
            try:
                with open(dashboard_path, "rb") as f:
                    part = MIMEApplication(f.read(), Name="tiuku_dashboard.html")
                part["Content-Disposition"] = 'attachment; filename="tiuku_dashboard.html"'
                msg.attach(part)
            except Exception as e:
                logger.error(f"Failed to attach HTML dashboard: {e}")

        # Attach Markdown report if provided
        if report_md_path and report_md_path.exists():
            try:
                with open(report_md_path, "rb") as f:
                    part = MIMEApplication(f.read(), Name=report_md_path.name)
                part["Content-Disposition"] = f'attachment; filename="{report_md_path.name}"'
                msg.attach(part)
            except Exception as e:
                logger.error(f"Failed to attach Markdown report: {e}")

        # Send via SMTP
        try:
            logger.info(f"Connecting to SMTP server {self.smtp_server}:{self.smtp_port}...")
            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
                if self.smtp_port == 587:
                    try:
                        server.starttls()
                    except Exception as e:
                        logger.warning(f"STARTTLS warning: {e}")

            # Perform login if valid username & password are supplied
            pwd_lower = str(self.password).lower()
            if self.username and self.password and "syötä-tähän" not in pwd_lower and "your-" not in pwd_lower:
                try:
                    server.login(self.username, self.password)
                except Exception as e:
                    logger.warning(f"SMTP authentication skipped or failed: {e}")

            server.sendmail(self.email_from or self.username, [self.email_to], msg.as_string())
            server.quit()

            logger.info(f"✅ Report email successfully sent to {self.email_to}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to send report email via SMTP: {e}")
            return False

    def send_urgent_alert_email(
        self,
        triggers: list,
        portfolio_summary: Dict[str, Any],
        ai_evaluations: Optional[list] = None,
    ) -> bool:
        """Sends an urgent real-time alert email when Stop-Loss, Take-Profit or Volatility triggers breach thresholds."""
        if not self.is_configured():
            logger.warning("SMTP email configuration incomplete. Skipping urgent alert email sending.")
            return False

        currency = portfolio_summary.get("currency", "EUR")
        date_str = datetime.now().strftime("%d.%m.%Y klo %H:%M")
        
        triggered_names = []
        for t in triggers:
            n = t.get("name") or t.get("symbol")
            if n:
                triggered_names.append(n)
        names_str = ", ".join(list(dict.fromkeys(triggered_names)))
        subject = f"📊 [tradeBotTiuku] Hintahälytys: {names_str} ({date_str})"

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = self.email_from or self.username or "tiuku@local"
        msg["To"] = self.email_to

        triggers_html = ""
        for idx, trg in enumerate(triggers, 1):
            t_type = trg.get("type", "MARKET_ALERT")
            badge_color = "#e11d48" if "STOP_LOSS" in t_type else ("#16a34a" if "TAKE_PROFIT" in t_type else "#d97706")
            type_label = "STOP-LOSS" if "STOP_LOSS" in t_type else ("TAKE-PROFIT" if "TAKE_PROFIT" in t_type else "VOLATILITEETTI")

            sym = trg.get("symbol", "")
            name = trg.get("name", "")
            display_title = trg.get("display_name") or (f"{name} ({sym})" if name and name != sym else sym)

            triggers_html += f"""
            <div style="background: #ffffff; border-left: 4px solid {badge_color}; border: 1px solid #e2e8f0; border-left-width: 5px; padding: 14px; margin-bottom: 12px; border-radius: 6px;">
                <span style="background: {badge_color}; color: #ffffff; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-size: 0.8rem;">{type_label}</span>
                <strong style="font-size: 1.05rem; margin-left: 8px; color: #0f172a;">{display_title}</strong>
                <p style="margin: 6px 0 0 0; color: #334155;">{trg.get('message')}</p>
            </div>
            """

        ai_eval_html = ""
        if ai_evaluations:
            ai_eval_html = "<h3 style='color: #0f172a; margin-top: 20px;'>🧠 AI Advisor -herätyspisteet</h3><ul>"
            holdings = portfolio_summary.get("holdings", {})
            for ev in ai_evaluations:
                ev_sym = ev.get('symbol', '')
                ev_name = holdings.get(ev_sym, {}).get("name") or ev_sym
                ev_disp = f"{ev_name} ({ev_sym})" if ev_name and ev_name != ev_sym else ev_sym
                ai_eval_html += f"<li><strong>{ev_disp}</strong>: AI-pisteet {ev.get('ai_score', 'N/A')}/10 — {ev.get('recommendation', 'N/A')} ({ev.get('reasoning', '')})</li>"
            ai_eval_html += "</ul>"

        body_html = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; max-width: 650px; margin: 0 auto; padding: 20px;">
            <div style="background: #1e293b; color: #f8fafc; padding: 20px; border-radius: 10px; margin-bottom: 20px;">
                <h2 style="color: #38bdf8; margin: 0 0 8px 0;">🔔 MARKKINAVAHDI HINTAHÄLYTYS</h2>
                <p style="margin: 0; font-size: 0.9rem; color: #94a3b8;">Aika: {date_str} — Markkinavahti havaitsi hintamuutoksia</p>
            </div>

            <h3 style="color: #0f172a;">Aktivoidut Liipaisimet:</h3>
            {triggers_html}

            {ai_eval_html}

            <div style="background: #f8fafc; border: 1px solid #cbd5e1; padding: 14px; border-radius: 6px; margin-top: 20px; font-size: 0.88rem; color: #475569;">
                ℹ️ <em>Tämä ilmoitus lähetettiin hinnanmuutoksen johdosta. Voit tarkastella salkkusi tilannetta tiuku_dashboard.html -sivulta.</em>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(body_html, "html", "utf-8"))

        try:
            logger.info(f"Connecting to SMTP server {self.smtp_server}:{self.smtp_port} for urgent alert...")
            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
                if self.smtp_port == 587:
                    try:
                        server.starttls()
                    except Exception as e:
                        logger.warning(f"STARTTLS warning: {e}")

            pwd_lower = str(self.password).lower()
            if self.username and self.password and "syötä-tähän" not in pwd_lower and "your-" not in pwd_lower:
                try:
                    server.login(self.username, self.password)
                except Exception as e:
                    logger.warning(f"SMTP login failed: {e}")

            server.sendmail(self.email_from or self.username, [self.email_to], msg.as_string())
            server.quit()
            logger.info(f"🚨 Urgent alert email successfully sent to {self.email_to}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to send urgent alert email: {e}")
            return False

    def send_screener_alert_email(
        self,
        pipeline_type: str,
        ticker: str,
        company_name: str,
        analysis_summary: Dict[str, Any],
    ) -> bool:
        """
        Sends a cleanly formatted trade alert email for Core or Satellite signals.
        """
        if not self.is_configured():
            logger.warning("SMTP email configuration incomplete (check SMTP_SERVER, EMAIL_TO). Skipping screener alert email.")
            return False

        pipe = pipeline_type.upper().strip()
        signal_title = analysis_summary.get("signal_title")
        if not signal_title:
            signal_title = "Value Setup" if pipe == "CORE" else "Strong Buy"

        # 1. Subject Line
        # E.g. [SATELLITE ALERT] Strong Buy: RAUTE.HE or [CORE ALERT] Value Setup: KEMIRA.HE
        subject = f"[{pipe} ALERT] {signal_title}: {ticker}"

        # 2. Extract Data
        reasoning = analysis_summary.get("reasoning", "")
        signals = analysis_summary.get("signals", "")
        allocation_eur = analysis_summary.get("recommended_allocation_eur")
        sizing_model = analysis_summary.get("sizing_model", "")
        spread_pct = analysis_summary.get("spread_pct")
        bid = analysis_summary.get("bid")
        ask = analysis_summary.get("ask")
        volume = analysis_summary.get("volume")
        total_friction_pct = analysis_summary.get("total_friction_pct")
        min_trade = analysis_summary.get("min_recommended_trade_eur")
        link = analysis_summary.get("link", "")
        title = analysis_summary.get("title", "")

        # Strategy specific fields
        gm_pct = analysis_summary.get("gross_margin_pct")
        recurring_rev = analysis_summary.get("recurring_revenue")
        r40_score = analysis_summary.get("rule_of_40_score")
        insider_buying = analysis_summary.get("insider_buying_personal", analysis_summary.get("management_buying"))
        pos_guidance = analysis_summary.get("positive_guidance")

        # Color palette
        if pipe == "CORE":
            badge_color = "#2563eb"
            bg_gradient = "linear-gradient(135deg, #1e3a8a 0%, #1e40af 100%)"
            pipeline_label = "CORE STRATEGY (10-Bagger Fundamental Setup)"
        else:
            badge_color = "#059669"
            bg_gradient = "linear-gradient(135deg, #064e3b 0%, #047857 100%)"
            pipeline_label = "SATELLITE STRATEGY (Daily Catalyst Hunting)"

        # 3. Plain Text Body
        plain_lines = [
            f"=== [{pipe} ALERT] {signal_title}: {ticker} ===",
            f"Company:      {company_name}",
            f"Pipeline:     {pipeline_label}",
            f"Signals:      {signals or signal_title}",
            "",
            "--- LLM REASONING & ANALYSIS ---",
            f"{reasoning or 'Ei lisäperusteluja.'}",
            "",
            "--- TRADE SPECIFICATIONS ---",
        ]
        if allocation_eur is not None:
            plain_lines.append(f"Position Size: {allocation_eur:,.0f} EUR ({sizing_model})")
        if spread_pct is not None:
            plain_lines.append(f"Spread:        {spread_pct:.2f}% (Bid: {bid} / Ask: {ask})")
        if total_friction_pct is not None:
            plain_lines.append(f"Total Friction: {total_friction_pct:.2f}%")
        if min_trade is not None:
            plain_lines.append(f"Min. Trade:    {min_trade:,.0f} EUR")
        if title:
            plain_lines.append(f"Disclosure:    {title}")
        if link:
            plain_lines.append(f"Source Link:   {link}")

        plain_text = "\n".join(plain_lines)

        # 4. HTML Table of Metrics
        metrics_rows = []
        if pipe == "CORE":
            if gm_pct is not None:
                metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Myyntikate (Gross Margin):</td><td style='padding: 8px 12px;'>{gm_pct:.1f}%</td></tr>")
            if recurring_rev is not None:
                metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Toistuva liikevaihto (SaaS/ARR):</td><td style='padding: 8px 12px;'>{'Kyllä' if recurring_rev else 'Ei'}</td></tr>")
            if r40_score is not None:
                metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Rule of 40:</td><td style='padding: 8px 12px;'>{r40_score:.1f}%</td></tr>")
        else:
            if insider_buying is not None:
                metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Sisäpiirin henkilökohtainen osto:</td><td style='padding: 8px 12px;'>{'Kyllä' if insider_buying else 'Ei'}</td></tr>")
            if pos_guidance is not None:
                metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Positiivinen tulosvaroitus:</td><td style='padding: 8px 12px;'>{'Kyllä' if pos_guidance else 'Ei'}</td></tr>")

        if allocation_eur is not None:
            metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Suositeltu positio:</td><td style='padding: 8px 12px;'><strong>{allocation_eur:,.0f} EUR</strong> ({sizing_model})</td></tr>")
        if spread_pct is not None:
            metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Spread & Volyymi:</td><td style='padding: 8px 12px;'>{spread_pct:.2f}% (Vol: {volume or 'N/A'})</td></tr>")
        if total_friction_pct is not None:
            metrics_rows.append(f"<tr><td style='padding: 8px 12px; font-weight: bold;'>Kaupankäynnin kokonaiskitka:</td><td style='padding: 8px 12px;'>{total_friction_pct:.2f}% (Min. suositus >= {min_trade or 500:,.0f} EUR)</td></tr>")

        metrics_table_html = "<table style='width: 100%; border-collapse: collapse; margin-top: 10px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;'>" + "".join(metrics_rows) + "</table>" if metrics_rows else ""

        link_html = f"<p style='margin-top: 18px;'><a href='{link}' style='display: inline-block; background: {badge_color}; color: #ffffff; padding: 10px 18px; border-radius: 6px; text-decoration: none; font-weight: bold;'>🔗 Avaa Pörssitiedote</a></p>" if link else ""

        body_html = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; max-width: 650px; margin: 0 auto; padding: 20px; background-color: #f8fafc;">
            <div style="background: {bg_gradient}; color: #ffffff; padding: 22px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
                <div style="font-size: 0.85rem; font-weight: bold; letter-spacing: 0.05em; text-transform: uppercase; color: #cbd5e1; margin-bottom: 6px;">{pipeline_label}</div>
                <h1 style="margin: 0; font-size: 1.6rem; color: #ffffff;">{company_name} <span style="background: rgba(255,255,255,0.2); padding: 2px 8px; border-radius: 4px; font-size: 1.1rem; margin-left: 6px;">{ticker}</span></h1>
            </div>

            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; color: #0f172a; border-bottom: 2px solid #f1f5f9; padding-bottom: 8px;">🎯 Signaali & Kaupankäyntiparametrit</h3>
                <p style="margin: 6px 0;"><strong>Signaali:</strong> <span style="color: {badge_color}; font-weight: bold;">{signals or signal_title}</span></p>
                {metrics_table_html}
            </div>

            <div style="background: #ffffff; border-left: 4px solid {badge_color}; border: 1px solid #e2e8f0; border-left-width: 5px; border-radius: 8px; padding: 18px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; color: #0f172a;">🧠 LLM Analyysi & Pedagoginen Perustelu</h3>
                <p style="margin: 0; font-size: 0.95rem; color: #334155; line-height: 1.6; white-space: pre-line;">{reasoning or 'Ei lisäperusteluja saatavilla.'}</p>
            </div>

            {link_html}

            <p style="font-size: 0.8rem; color: #94a3b8; margin-top: 30px; text-align: center; border-top: 1px solid #e2e8f0; padding-top: 15px;">
                tradeBotTiuku Screener • Dual-Pipeline Core & Satellite System
            </p>
        </body>
        </html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.email_from or self.username or "tiuku@local"
        msg["To"] = self.email_to

        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        try:
            logger.info(f"Sending [{pipe} ALERT] email for {ticker} to {self.email_to}...")
            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
                if self.smtp_port == 587:
                    try:
                        server.starttls()
                    except Exception as e:
                        logger.warning(f"STARTTLS warning: {e}")

            pwd_lower = str(self.password).lower()
            if self.username and self.password and "syötä-tähän" not in pwd_lower and "your-" not in pwd_lower:
                try:
                    server.login(self.username, self.password)
                except Exception as e:
                    logger.warning(f"SMTP login skipped or failed: {e}")

            server.sendmail(self.email_from or self.username, [self.email_to], msg.as_string())
            server.quit()
            logger.info(f"✅ [{pipe} ALERT] email successfully sent for {ticker} to {self.email_to}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to send screener alert email for {ticker}: {e}")
            return False

    def send_screener_sell_alert_email(
        self,
        ticker: str,
        strategy_type: str,
        trade_data: Dict[str, Any],
    ) -> bool:
        """
        Sends an automated [SELL ALERT] email notification when an open position is exited.
        """
        if not self.is_configured:
            logger.debug("Email client not configured. Skipping sell alert email.")
            return False

        strat = strategy_type.upper().strip()
        entry_price = float(trade_data.get("EntryPrice", 0.0))
        exit_price_raw = float(trade_data.get("ExitPriceRaw", 0.0))
        exit_price_exec = float(trade_data.get("ExitPriceExec", exit_price_raw))
        net_return_pct = float(trade_data.get("NetReturnPct", 0.0))
        net_pnl_eur = float(trade_data.get("NetPnLEur", 0.0))
        holding_days = int(trade_data.get("HoldingDays", 0))
        exit_reason = trade_data.get("ExitReason", "Exit Condition Met")
        entry_date = trade_data.get("EntryDate", "")
        exit_date = trade_data.get("ExitDate", "")

        is_win = net_return_pct >= 0
        badge_color = "#10b981" if is_win else "#ef4444"
        subject = f"[SELL ALERT] {strat} Exit: {ticker} ({net_return_pct:+.1f}%)"

        plain_text = f"""
===================================================================
[SELL ALERT] {strat} POSITION CLOSED: {ticker}
===================================================================
Exit Reason:        {exit_reason}
Realized Net PnL:   {net_return_pct:+.2f}% ({net_pnl_eur:+.2f} EUR)
Holding Period:     {holding_days} days (Entry: {entry_date} -> Exit: {exit_date})
Entry Price:        {entry_price:.2f} EUR
Exit Price (Exec):  {exit_price_exec:.2f} EUR (Raw: {exit_price_raw:.2f} EUR)
===================================================================
tradeBotTiuku Exit Manager
        """.strip()

        body_html = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #1e293b; max-width: 650px; margin: 0 auto; padding: 20px; background-color: #f8fafc;">
            <div style="background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: #ffffff; padding: 22px; border-radius: 10px; margin-bottom: 20px;">
                <div style="font-size: 0.85rem; font-weight: bold; letter-spacing: 0.05em; text-transform: uppercase; color: #cbd5e1; margin-bottom: 6px;">{strat} STRATEGY EXIT</div>
                <h1 style="margin: 0; font-size: 1.6rem; color: #ffffff;">[SELL ALERT] {ticker} <span style="background: {badge_color}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 1.1rem; margin-left: 6px;">{net_return_pct:+.1f}%</span></h1>
            </div>

            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; color: #0f172a; border-bottom: 2px solid #f1f5f9; padding-bottom: 8px;">📊 Kaupan Tiedot & Tuotto</h3>
                <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
                    <tr><td style="padding: 8px 12px; font-weight: bold;">Syy:</td><td style="padding: 8px 12px; color: {badge_color}; font-weight: bold;">{exit_reason}</td></tr>
                    <tr><td style="padding: 8px 12px; font-weight: bold;">Nettotuotto:</td><td style="padding: 8px 12px;"><strong>{net_return_pct:+.2f}% ({net_pnl_eur:+.2f} EUR)</strong></td></tr>
                    <tr><td style="padding: 8px 12px; font-weight: bold;">Pitoaika:</td><td style="padding: 8px 12px;">{holding_days} päivää ({entry_date} &rarr; {exit_date})</td></tr>
                    <tr><td style="padding: 8px 12px; font-weight: bold;">Ostohinta:</td><td style="padding: 8px 12px;">{entry_price:.2f} EUR</td></tr>
                    <tr><td style="padding: 8px 12px; font-weight: bold;">Myyntihinta:</td><td style="padding: 8px 12px;">{exit_price_exec:.2f} EUR (kulujen jälkeen)</td></tr>
                </table>
            </div>

            <p style="font-size: 0.8rem; color: #94a3b8; margin-top: 30px; text-align: center; border-top: 1px solid #e2e8f0; padding-top: 15px;">
                tradeBotTiuku Exit Manager • Dual-Pipeline Core & Satellite System
            </p>
        </body>
        </html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.email_from or self.username or "tiuku@local"
        msg["To"] = self.email_to

        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        try:
            logger.info(f"Sending [SELL ALERT] email for {ticker} to {self.email_to}...")
            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
                if self.smtp_port == 587:
                    try:
                        server.starttls()
                    except Exception as e:
                        logger.warning(f"STARTTLS warning: {e}")

            pwd_lower = str(self.password).lower()
            if self.username and self.password and "syötä-tähän" not in pwd_lower and "your-" not in pwd_lower:
                try:
                    server.login(self.username, self.password)
                except Exception as e:
                    logger.warning(f"SMTP login skipped or failed: {e}")

            server.sendmail(self.email_from or self.username, [self.email_to], msg.as_string())
            server.quit()
            logger.info(f"✅ [SELL ALERT] email successfully sent for {ticker} to {self.email_to}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to send sell alert email for {ticker}: {e}")
            return False


def send_alert(
    pipeline_type: str,
    ticker: str,
    company_name: str,
    analysis_summary: Dict[str, Any],
    email_client: Optional[EmailClient] = None,
) -> bool:
    """
    Convenience function to send a formatted trade alert email.
    """
    client = email_client or EmailClient()
    return client.send_screener_alert_email(
        pipeline_type=pipeline_type,
        ticker=ticker,
        company_name=company_name,
        analysis_summary=analysis_summary,
    )


def send_sell_alert(
    ticker: str,
    strategy_type: str,
    trade_data: Dict[str, Any],
    email_client: Optional[EmailClient] = None,
) -> bool:
    """
    Convenience function to send an automated sell alert email.
    """
    client = email_client or EmailClient()
    return client.send_screener_sell_alert_email(
        ticker=ticker,
        strategy_type=strategy_type,
        trade_data=trade_data,
    )


