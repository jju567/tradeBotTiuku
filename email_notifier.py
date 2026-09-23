"""
email_notifier.py - Lightweight Email Alert Module for Multi-Portfolio Trading Bot.

Triggers email notifications on BUY, SELL, and WARN events for individual portfolios.
Loads credentials from .env and environment variables. Fails gracefully without
interrupting trading execution if SMTP fails.
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("email_notifier")

# Locate .env in workspace root
_BASE_DIR = Path(__file__).resolve().parent
_ENV_FILE = _BASE_DIR / ".env"


def load_env_credentials() -> Dict[str, str]:
    """Loads SMTP credentials from .env and environment variables."""
    configs: Dict[str, str] = {}
    if _ENV_FILE.exists():
        try:
            with open(_ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        configs[k.strip()] = v.strip().strip('"').strip("'")
        except Exception as e:
            logger.debug(f"Could not parse .env: {e}")

    # Fallback to os.environ
    smtp_server = os.environ.get("SMTP_SERVER", configs.get("SMTP_SERVER", ""))
    smtp_port_raw = os.environ.get("SMTP_PORT", configs.get("SMTP_PORT", "587"))
    try:
        smtp_port = int(smtp_port_raw)
    except (ValueError, TypeError):
        smtp_port = 587

    smtp_user = (
        os.environ.get("SMTP_USER")
        or configs.get("SMTP_USER")
        or os.environ.get("SMTP_USERNAME")
        or configs.get("SMTP_USERNAME")
        or ""
    )
    smtp_pass = (
        os.environ.get("SMTP_PASS")
        or configs.get("SMTP_PASS")
        or os.environ.get("SMTP_PASSWORD")
        or configs.get("SMTP_PASSWORD")
        or ""
    )
    alert_email = (
        os.environ.get("ALERT_EMAIL")
        or configs.get("ALERT_EMAIL")
        or os.environ.get("EMAIL_TO")
        or configs.get("EMAIL_TO")
        or ""
    )

    return {
        "SMTP_SERVER": smtp_server,
        "SMTP_PORT": str(smtp_port),
        "SMTP_USER": smtp_user,
        "SMTP_PASS": smtp_pass,
        "ALERT_EMAIL": alert_email,
    }


def send_portfolio_alert(
    portfolio_id: str,
    event_type: str,
    ticker: str,
    details: Optional[Dict[str, Any]] = None,
    subject: Optional[str] = None,
) -> bool:
    """
    Sends an immediate email notification for a portfolio trade or warning.

    Parameters:
        portfolio_id: Name/ID of portfolio (e.g. 'P5_Nordic_Only')
        event_type: 'BUY', 'SELL', or 'WARN'
        ticker: Symbol (e.g. 'EXEL.HE')
        details: Optional dict with price, shares, fees, PnL, reasoning, etc.
        subject: Optional custom subject override

    Returns:
        True if email was sent successfully, False otherwise (fails gracefully).
    """
    creds = load_env_credentials()
    smtp_server = creds.get("SMTP_SERVER", "").strip()
    smtp_port = int(creds.get("SMTP_PORT", "587"))
    smtp_user = creds.get("SMTP_USER", "").strip()
    smtp_pass = creds.get("SMTP_PASS", "").strip()
    alert_email = creds.get("ALERT_EMAIL", "").strip()

    if not smtp_server or not alert_email:
        logger.info(
            f"SMTP not configured (SMTP_SERVER='{smtp_server}', ALERT_EMAIL='{alert_email}'). "
            f"Skipping email alert for [{portfolio_id}] {event_type} {ticker}."
        )
        return False

    clean_event = event_type.strip().upper()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Format Subject according to CRITICAL requirement
    if not subject:
        subject = f"TRADE BOT [{portfolio_id}]: {clean_event} {ticker}"

    details = details or {}

    # Format Body (Plain text and HTML)
    text_lines = [
        f"TRADE BOT NOTIFICATION",
        f"========================================",
        f"Portfolio:   {portfolio_id}",
        f"Action:      {clean_event}",
        f"Ticker:      {ticker}",
        f"Timestamp:   {now_str}",
        f"----------------------------------------",
    ]
    for k, v in details.items():
        text_lines.append(f"{k.replace('_', ' ').capitalize():<16}: {v}")
    text_lines.append(f"========================================")
    body_text = "\n".join(text_lines)

    # HTML body
    event_color = {
        "BUY": "#16a34a",
        "SELL": "#dc2626",
        "WARN": "#d97706",
    }.get(clean_event, "#2563eb")

    rows_html = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:600;color:#475569;'>{k.replace('_', ' ').capitalize()}</td>"
        f"<td style='padding:6px 12px;color:#0f172a;'>{v}</td></tr>"
        for k, v in details.items()
    )

    body_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1e293b;max-width:600px;margin:0 auto;padding:16px;">
  <div style="background:#0f172a;padding:16px 20px;border-radius:8px 8px 0 0;color:#f8fafc;">
    <h2 style="margin:0;font-size:1.25rem;">🐱 tradeBotTiuku Alert</h2>
    <p style="margin:4px 0 0 0;font-size:0.85rem;color:#94a3b8;">{now_str}</p>
  </div>
  <div style="border:1px solid #e2e8f0;border-top:none;padding:20px;border-radius:0 0 8px 8px;background:#ffffff;">
    <div style="display:inline-block;background:{event_color};color:#ffffff;padding:4px 10px;border-radius:4px;font-weight:700;font-size:0.9rem;margin-bottom:12px;">
      {clean_event} &bull; {ticker}
    </div>
    <p style="margin:0 0 12px 0;font-size:1.05rem;"><strong>Portfolio:</strong> <code>{portfolio_id}</code></p>
    <table style="width:100%;border-collapse:collapse;margin-top:10px;border:1px solid #f1f5f9;background:#f8fafc;border-radius:6px;overflow:hidden;">
      <tbody>
        {rows_html}
      </tbody>
    </table>
    <p style="margin-top:18px;font-size:0.8rem;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:12px;">
      Automated alert generated by tradeBotTiuku Multi-Portfolio Walk-Forward Engine.
    </p>
  </div>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user or "tradebot@localhost"
    msg["To"] = alert_email
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        logger.info(f"Sending [{portfolio_id}] {clean_event} alert for {ticker} to {alert_email}...")
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()

        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)

        server.sendmail(msg["From"], [alert_email], msg.as_string())
        server.quit()
        logger.info(f"Successfully sent alert email: {subject}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send email alert ({subject}): {e}. Continuing without interruption.")
        return False
