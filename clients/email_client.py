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

        alerts_list_html = ""
        if risk_alerts:
            for a in risk_alerts:
                alerts_list_html += f"<li>⚠️ <strong>{a['symbol']}</strong>: {a['message']} (Toimenpide: {a['recommended_action']})</li>"
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
