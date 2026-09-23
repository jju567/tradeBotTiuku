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


def send_run_summary_email(
    run_trades: List[Dict[str, Any]],
    portfolio_results: Dict[str, Dict[str, Any]],
    cycle_timestamp: Optional[str] = None,
) -> bool:
    """
    Sends a single consolidated email summary of all actions (BUY, SELL, WARN) executed
    during the trading daemon cycle across all portfolios.
    Only sends if there were executed trades/warnings, or logs summary info.
    """
    creds = load_env_credentials()
    smtp_server = creds.get("SMTP_SERVER", "").strip()
    smtp_port = int(creds.get("SMTP_PORT", "587"))
    smtp_user = creds.get("SMTP_USER", "").strip()
    smtp_pass = creds.get("SMTP_PASS", "").strip()
    alert_email = creds.get("ALERT_EMAIL", "").strip()

    if not smtp_server or not alert_email:
        logger.info("SMTP not configured. Skipping run summary email.")
        return False

    now_str = cycle_timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_buys = sum(1 for t in run_trades if t.get("action") == "BUY")
    total_sells = sum(1 for t in run_trades if t.get("action") == "SELL")
    total_warns = sum(1 for t in run_trades if t.get("action") == "WARN")

    if not run_trades:
        logger.info("No trades or warnings in this run. Skipping cycle summary email.")
        return False

    subject = f"TRADE BOT: Ajon yhteenveto — {total_buys} Ostoa, {total_sells} Myyntiä ({now_str})"

    # Plain text summary
    text_lines = [
        "TRADE BOT - AJON KAUPPOJEN YHTEENVETO",
        "=" * 70,
        f"Aika:               {now_str}",
        f"Toteutetut ostot:   {total_buys}",
        f"Toteutetut myynnit: {total_sells}",
        f"Varoitukset:        {total_warns}",
        "-" * 70,
        "TAPAHTUMAT:",
    ]

    for t in run_trades:
        action = t.get("action", "")
        pid = t.get("portfolio_id", "")
        ticker = t.get("ticker", "")
        details_str = ", ".join(f"{k}: {v}" for k, v in t.get("details", {}).items())
        text_lines.append(f"  • [{pid}] {action} {ticker} -> {details_str}")

    text_lines.append("-" * 70)
    text_lines.append("SALKKUJEN TILANNE AJON JÄLKEEN:")
    for pid, r in portfolio_results.items():
        text_lines.append(
            f"  • {pid:<22} | Pos: {r.get('positions_count', 0):<2} | "
            f"Käteinen: {r.get('cash', 0.0):>9,.2f} € | "
            f"Oma pääoma: {r.get('total_equity', 0.0):>10,.2f} € | "
            f"Tuotto: {r.get('return_pct', 0.0):>+6.2f}%"
        )
    text_lines.append("=" * 70)
    body_text = "\n".join(text_lines)

    # HTML table rows for trades
    trades_html_rows = ""
    for t in run_trades:
        action = t.get("action", "")
        action_color = "#16a34a" if action == "BUY" else ("#dc2626" if action == "SELL" else "#d97706")
        pid = t.get("portfolio_id", "")
        ticker = t.get("ticker", "")
        details_map = t.get("details", {})
        det_snippet = "<br>".join(f"<strong>{k.replace('_', ' ').capitalize()}:</strong> {v}" for k, v in details_map.items())
        trades_html_rows += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
          <td style="padding: 8px 12px; font-weight: 600; color: #1e293b;">{pid}</td>
          <td style="padding: 8px 12px;">
            <span style="background: {action_color}; color: #ffffff; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.85rem;">
              {action}
            </span>
          </td>
          <td style="padding: 8px 12px; font-weight: 700; color: #0f172a;">{ticker}</td>
          <td style="padding: 8px 12px; font-size: 0.88rem; color: #475569;">{det_snippet}</td>
        </tr>
        """

    # HTML table rows for portfolio statuses
    portfolios_html_rows = ""
    for pid, r in portfolio_results.items():
        ret = r.get("return_pct", 0.0)
        ret_color = "#16a34a" if ret >= 0 else "#dc2626"
        portfolios_html_rows += f"""
        <tr style="border-bottom: 1px solid #f1f5f9;">
          <td style="padding: 6px 10px; font-weight: 600;">{pid}</td>
          <td style="padding: 6px 10px; text-align: center;">{r.get('positions_count', 0)}</td>
          <td style="padding: 6px 10px; text-align: right;">{r.get('cash', 0.0):,.2f} €</td>
          <td style="padding: 6px 10px; text-align: right; font-weight: 600;">{r.get('total_equity', 0.0):,.2f} €</td>
          <td style="padding: 6px 10px; text-align: right; color: {ret_color}; font-weight: 700;">{ret:+.2f}%</td>
        </tr>
        """

    body_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1e293b;max-width:750px;margin:0 auto;padding:16px;">
  <div style="background:#0f172a;padding:18px 24px;border-radius:8px 8px 0 0;color:#f8fafc;">
    <h2 style="margin:0;font-size:1.35rem;">🐱 tradeBotTiuku — Ajon Kauppayhteenveto</h2>
    <p style="margin:6px 0 0 0;font-size:0.88rem;color:#94a3b8;">Aika: {now_str} &bull; Yhteensä {len(run_trades)} toimenpidettä ({total_buys} ostoa, {total_sells} myyntiä)</p>
  </div>
  <div style="border:1px solid #e2e8f0;border-top:none;padding:24px;border-radius:0 0 8px 8px;background:#ffffff;">
    <h3 style="margin-top:0;margin-bottom:12px;color:#0f172a;font-size:1.1rem;border-bottom:2px solid #f1f5f9;padding-bottom:6px;">
      🛒 Ajon Aikana Toteutetut Kaupat &amp; Tapahtumat
    </h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px;background:#f8fafc;border-radius:6px;overflow:hidden;border:1px solid #e2e8f0;">
      <thead>
        <tr style="background:#f1f5f9;text-align:left;color:#475569;font-size:0.85rem;text-transform:uppercase;">
          <th style="padding:8px 12px;">Salkku</th>
          <th style="padding:8px 12px;">Toiminto</th>
          <th style="padding:8px 12px;">Tikkeri</th>
          <th style="padding:8px 12px;">Tiedot</th>
        </tr>
      </thead>
      <tbody>
        {trades_html_rows}
      </tbody>
    </table>

    <h3 style="margin-top:20px;margin-bottom:12px;color:#0f172a;font-size:1.1rem;border-bottom:2px solid #f1f5f9;padding-bottom:6px;">
      📊 Salkkujen Tilanne Ajon Jälkeen
    </h3>
    <table style="width:100%;border-collapse:collapse;font-size:0.9rem;border:1px solid #e2e8f0;border-radius:6px;overflow:hidden;">
      <thead>
        <tr style="background:#f1f5f9;text-align:left;color:#475569;font-size:0.82rem;text-transform:uppercase;">
          <th style="padding:8px 10px;">Salkku</th>
          <th style="padding:8px 10px;text-align:center;">Positiot</th>
          <th style="padding:8px 10px;text-align:right;">Käteinen</th>
          <th style="padding:8px 10px;text-align:right;">Oma Pääoma</th>
          <th style="padding:8px 10px;text-align:right;">Tuotto %</th>
        </tr>
      </thead>
      <tbody>
        {portfolios_html_rows}
      </tbody>
    </table>

    <p style="margin-top:22px;font-size:0.8rem;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:12px;">
      Koontihälytys luotu automaattisesti tradeBotTiuku Multi-Portfolio Walk-Forward -moottorista.
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
        logger.info(f"Sending run summary email with {len(run_trades)} actions to {alert_email}...")
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()

        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)

        server.sendmail(msg["From"], [alert_email], msg.as_string())
        server.quit()
        logger.info(f"Successfully sent run summary email: {subject}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send run summary email ({subject}): {e}. Continuing without interruption.")
        return False

