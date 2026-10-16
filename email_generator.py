"""
email_generator.py - EOD Action List Email Generator for tradeBotTiuku.

Generates and sends human-execution-ready End-Of-Day (EOD) Action Lists
specifically optimized for Nordic & US trading on Finnish OST / retail accounts.
Categorizes orders into:
  - 🟢 BUYS (Enter as LIMIT orders before market open)
  - 🔴 FULL EXITS (Stop-Loss / Fundamental Breakdown / Time-Stop)
  - 🟡 PARTIAL TAKE-PROFIT (+25% Reached -> Sell 50% & Activate Runner Trailing Stop)

Includes regime status, investable cash, non-HODL equity, and clear HODL isolation.
"""

from __future__ import annotations

import logging
import math
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("email_generator")

# Import credential loader from email_notifier
try:
    from email_notifier import load_env_credentials
except ImportError:
    def load_env_credentials() -> Dict[str, str]:
        return {
            "SMTP_SERVER": os.environ.get("SMTP_SERVER", ""),
            "SMTP_PORT": os.environ.get("SMTP_PORT", "587"),
            "SMTP_USER": os.environ.get("SMTP_USER", ""),
            "SMTP_PASS": os.environ.get("SMTP_PASS", ""),
            "ALERT_EMAIL": os.environ.get("ALERT_EMAIL", ""),
        }


@dataclass
class EODActionOrder:
    """Represents a discrete executable trade order for the morning Action List."""
    category: str  # 'BUY', 'FULL_EXIT', 'PARTIAL_TP'
    ticker: str
    sub_strategy: str
    order_type: str = "LIMIT"
    exact_limit_price: float = 0.0
    currency: str = "EUR"
    share_count: int = 0
    estimated_total_eur: float = 0.0
    initial_stop_loss_price: str = "N/A"
    thesis_catalyst: str = ""

    def formatted_limit_price(self) -> str:
        return f"{self.exact_limit_price:.2f} {self.currency}"

    def formatted_estimated_eur(self) -> str:
        return f"{self.estimated_total_eur:,.2f} €"


@dataclass
class EODPortfolioSummary:
    """Portfolio state snapshot for EOD Action List header."""
    portfolio_id: str
    regime_status: str
    available_cash_eur: float
    total_equity_eur: float  # Excludes hodl=True positions
    hodl_positions: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_eod_action_orders(
    portfolio: Any,
    run_trades: Optional[List[Dict[str, Any]]] = None,
    fx_rates: Optional[Dict[str, float]] = None,
    limit_buffer_pct: float = 0.002,
) -> Tuple[List[EODActionOrder], List[EODActionOrder], List[EODActionOrder]]:
    """
    Parses executed or pending run trades and portfolio state into categorized EODActionOrder objects:
      (buys, full_exits, partial_tps)
    """
    fx_rates = fx_rates or {"EURUSD": 1.08, "EURSEK": 11.30, "EURDKK": 7.46, "EURNOK": 11.60}
    buys: List[EODActionOrder] = []
    full_exits: List[EODActionOrder] = []
    partial_tps: List[EODActionOrder] = []

    if not run_trades:
        return buys, full_exits, partial_tps

    pid = getattr(getattr(portfolio, "config", None), "portfolio_id", "P13_Live_Candidate")

    for t in run_trades:
        if t.get("portfolio_id") and t.get("portfolio_id") != pid:
            continue

        action = str(t.get("action", "")).upper()
        ticker = str(t.get("ticker", "")).strip().upper()
        details = t.get("details", {})

        # Extract native currency
        curr = details.get("currency") or ("USD" if ("." not in ticker) else ("SEK" if ticker.endswith(".ST") else "EUR"))

        # Calculate FX rate to EUR
        fx_to_eur = 1.0
        if curr == "USD":
            fx_to_eur = 1.0 / fx_rates.get("EURUSD", 1.08)
        elif curr == "SEK":
            fx_to_eur = 1.0 / fx_rates.get("EURSEK", 11.30)
        elif curr == "DKK":
            fx_to_eur = 1.0 / fx_rates.get("EURDKK", 7.46)
        elif curr == "NOK":
            fx_to_eur = 1.0 / fx_rates.get("EURNOK", 11.60)

        sub_strategy = details.get("sub_strategy") or details.get("Sub_Strategy") or "core_nordic_midcap_quality"

        if action == "BUY":
            shares = int(details.get("shares", 0))
            raw_price = details.get("buy_price")
            if isinstance(raw_price, str):
                try:
                    price_val = float(raw_price.split()[0].replace(",", ""))
                except Exception:
                    price_val = 0.0
            else:
                price_val = float(raw_price or 0.0)

            # Limit Price = Prev Close * (1 + buffer)
            exact_limit = details.get("limit_price")
            if exact_limit is None:
                exact_limit = round(price_val * (1.0 + limit_buffer_pct), 2)
            else:
                exact_limit = float(exact_limit)

            est_eur = details.get("estimated_total_eur")
            if est_eur is None:
                est_eur = round(shares * exact_limit * fx_to_eur, 2)
            else:
                est_eur = float(est_eur)

            # Stop loss calculation
            if "core" in sub_strategy.lower():
                stop_price = exact_limit * 0.85
                stop_str = f"{stop_price:.2f} {curr} (-15.0% Core Stop)"
            else:
                stop_price = exact_limit * 0.88
                stop_str = f"{stop_price:.2f} {curr} (-12.0% Satellite Stop)"

            thesis = details.get("thesis") or details.get("universe_filter") or "Tiered Filter Match & Catalyst"

            buys.append(
                EODActionOrder(
                    category="BUY",
                    ticker=ticker,
                    sub_strategy=sub_strategy,
                    order_type="LIMIT",
                    exact_limit_price=exact_limit,
                    currency=curr,
                    share_count=shares,
                    estimated_total_eur=est_eur,
                    initial_stop_loss_price=stop_str,
                    thesis_catalyst=thesis,
                )
            )

        elif action == "PARTIAL_TP":
            shares = int(details.get("sold_shares") or details.get("shares", 0))
            raw_price = details.get("sell_price")
            if isinstance(raw_price, str):
                try:
                    price_val = float(raw_price.split()[0].replace(",", ""))
                except Exception:
                    price_val = 0.0
            else:
                price_val = float(raw_price or 0.0)

            exact_limit = details.get("limit_price")
            if exact_limit is None:
                exact_limit = round(price_val * (1.0 - limit_buffer_pct), 2)
            else:
                exact_limit = float(exact_limit)

            est_eur = details.get("estimated_total_eur")
            if est_eur is None:
                est_eur = round(shares * exact_limit * fx_to_eur, 2)
            else:
                est_eur = float(est_eur)

            entry_px = details.get("entry_price") or details.get("buy_price")
            if entry_px:
                stop_str = f"Move Stop to Break-Even ({entry_px} {curr}) + Trailing -15.0%"
            else:
                stop_str = "Move Stop to Break-Even (0.0%) + Trailing -15.0%"

            thesis = details.get("exit_reason") or "+25.0% Gain Reached -> Sell 50% & Lock Profit; Activate Runner Trailing Stop"

            partial_tps.append(
                EODActionOrder(
                    category="PARTIAL_TP",
                    ticker=ticker,
                    sub_strategy=sub_strategy,
                    order_type="LIMIT",
                    exact_limit_price=exact_limit,
                    currency=curr,
                    share_count=shares,
                    estimated_total_eur=est_eur,
                    initial_stop_loss_price=stop_str,
                    thesis_catalyst=thesis,
                )
            )

        elif action == "SELL":
            shares = int(details.get("shares", 0))
            raw_price = details.get("sell_price")
            if isinstance(raw_price, str):
                try:
                    price_val = float(raw_price.split()[0].replace(",", ""))
                except Exception:
                    price_val = 0.0
            else:
                price_val = float(raw_price or 0.0)

            exact_limit = details.get("limit_price")
            if exact_limit is None:
                exact_limit = round(price_val * (1.0 - limit_buffer_pct), 2)
            else:
                exact_limit = float(exact_limit)

            est_eur = details.get("estimated_total_eur")
            if est_eur is None:
                est_eur = round(shares * exact_limit * fx_to_eur, 2)
            else:
                est_eur = float(est_eur)

            reason = details.get("exit_reason") or details.get("reason") or "Stop-Loss / Fundamental Deterioration"

            full_exits.append(
                EODActionOrder(
                    category="FULL_EXIT",
                    ticker=ticker,
                    sub_strategy=sub_strategy,
                    order_type="LIMIT",
                    exact_limit_price=exact_limit,
                    currency=curr,
                    share_count=shares,
                    estimated_total_eur=est_eur,
                    initial_stop_loss_price="EXIT_ORDER",
                    thesis_catalyst=reason,
                )
            )

    return buys, full_exits, partial_tps


def build_portfolio_summary(
    portfolio: Any,
    regime_data: Optional[Dict[str, Any]] = None,
    fx_rates: Optional[Dict[str, float]] = None,
    prices_cache: Optional[Dict[str, float]] = None,
) -> EODPortfolioSummary:
    """Extracts regime status, available cash, non-HODL total equity, and HODL positions."""
    fx_rates = fx_rates or {"EURUSD": 1.08, "EURSEK": 11.30, "EURDKK": 7.46, "EURNOK": 11.60}
    regime_data = regime_data or {}

    regime_str = str(regime_data.get("regime", "NEUTRAL / RANGE-BOUND")).upper()
    iwc_px = float(regime_data.get("last_price", 0.0) or 0.0)
    sma_50 = float(regime_data.get("sma_50", 0.0) or 0.0)
    if iwc_px > 0 and sma_50 > 0:
        regime_detail = f"{regime_str} (IWC {iwc_px:.2f} vs SMA50 {sma_50:.2f})"
    else:
        regime_detail = regime_str

    pid = getattr(getattr(portfolio, "config", None), "portfolio_id", "P13_Live_Candidate")
    cash_balance = float(getattr(portfolio, "cash_balance", 0.0))

    # Identify HODL positions vs regular positions
    hodl_positions: List[Dict[str, Any]] = []
    regular_positions: List[Dict[str, Any]] = []

    for pos in getattr(portfolio, "positions", []):
        ticker = str(pos.get("Ticker", "")).upper()
        is_hodl = bool(pos.get("hodl", False)) or ticker in ["FARON.HE", "FARON"] or pos.get("note") == "Lottolappu"
        if is_hodl:
            hodl_positions.append(pos)
        else:
            regular_positions.append(pos)

    # Calculate non-HODL equity using current live prices if available
    non_hodl_stock_eur = 0.0
    for pos in regular_positions:
        shares = float(pos.get("Shares", 0))
        ticker = str(pos.get("Ticker", "")).upper()
        px = None
        for key in ("current_price", "Current Price", "last_price", "Last Price"):
            val = pos.get(key)
            if val is not None:
                try:
                    fval = float(val)
                    if fval > 0:
                        px = fval
                        break
                except Exception:
                    pass
        if px is None and prices_cache:
            val = prices_cache.get(ticker) or prices_cache.get(ticker.upper())
            if val is not None:
                try:
                    fval = float(val)
                    if fval > 0:
                        px = fval
                except Exception:
                    pass
        if px is None:
            px = float(pos.get("Buy Price", 0.0) or 0.0)

        curr = pos.get("Currency", "EUR")
        fx = 1.0
        if curr == "USD":
            fx = 1.0 / fx_rates.get("EURUSD", 1.08)
        elif curr == "SEK":
            fx = 1.0 / fx_rates.get("EURSEK", 11.30)
        elif curr == "DKK":
            fx = 1.0 / fx_rates.get("EURDKK", 7.46)
        elif curr == "NOK":
            fx = 1.0 / fx_rates.get("EURNOK", 11.60)
        non_hodl_stock_eur += shares * px * fx

    total_p13_equity = cash_balance + non_hodl_stock_eur

    return EODPortfolioSummary(
        portfolio_id=pid,
        regime_status=regime_detail,
        available_cash_eur=cash_balance,
        total_equity_eur=total_p13_equity,
        hodl_positions=hodl_positions,
    )


def format_eod_action_list_text(
    summary: EODPortfolioSummary,
    buys: List[EODActionOrder],
    full_exits: List[EODActionOrder],
    partial_tps: List[EODActionOrder],
) -> str:
    """Formats the plain text version of the EOD Action List email."""
    lines: List[str] = []
    lines.append("=" * 80)
    lines.append(f"🐱 tradeBotTiuku — EOD Action List: {summary.portfolio_id}")
    lines.append(f"Päivämäärä: {summary.timestamp}")
    lines.append("=" * 80)
    lines.append("")
    lines.append("PORTFOLIO HEADER:")
    lines.append(f"  • Regime Status:              {summary.regime_status}")
    lines.append(f"  • Available Cash:             {summary.available_cash_eur:,.2f} €")
    lines.append(f"  • Total P13 Equity (ex-HODL): {summary.total_equity_eur:,.2f} €")

    if summary.hodl_positions:
        hodl_str = ", ".join(f"{p.get('Ticker')}: Ignored by bot (HODL=True)" for p in summary.hodl_positions)
        lines.append(f"  • HODL Protected Positions:   {hodl_str}")
    else:
        lines.append("  • HODL Protected Positions:   None active (FARON.HE protected if added)")

    lines.append("")
    lines.append("-" * 80)

    # 1. BUYS
    lines.append(f"🟢 BUYS ({len(buys)}) — Enter as LIMIT orders before market open")
    lines.append("-" * 80)
    if not buys:
        lines.append("  (Ei uusia ostotoimeksiantoja)")
    else:
        for idx, b in enumerate(buys, 1):
            lines.append(f"  {idx}. TICKER:                  {b.ticker}")
            lines.append(f"     SUB_STRATEGY:            {b.sub_strategy}")
            lines.append(f"     ORDER_TYPE:              {b.order_type}")
            lines.append(f"     EXACT_LIMIT_PRICE:       {b.formatted_limit_price()}")
            lines.append(f"     SHARE_COUNT:             {b.share_count}")
            lines.append(f"     ESTIMATED_TOTAL_EUR:     {b.formatted_estimated_eur()}")
            lines.append(f"     INITIAL_STOP_LOSS_PRICE: {b.initial_stop_loss_price}")
            lines.append(f"     THESIS / CATALYST:       {b.thesis_catalyst}")
            lines.append("")

    # 2. FULL EXITS
    lines.append("-" * 80)
    lines.append(f"🔴 FULL EXITS ({len(full_exits)}) — Stop-Loss / Fundamental Breakdown / Time-Stop")
    lines.append("-" * 80)
    if not full_exits:
        lines.append("  (Ei täysiä myyntitoimeksiantoja)")
    else:
        for idx, e in enumerate(full_exits, 1):
            lines.append(f"  {idx}. TICKER:                  {e.ticker}")
            lines.append(f"     SUB_STRATEGY:            {e.sub_strategy}")
            lines.append(f"     ORDER_TYPE:              {e.order_type}")
            lines.append(f"     EXACT_LIMIT_PRICE:       {e.formatted_limit_price()}")
            lines.append(f"     SHARE_COUNT:             {e.share_count}")
            lines.append(f"     ESTIMATED_TOTAL_EUR:     {e.formatted_estimated_eur()}")
            lines.append(f"     INITIAL_STOP_LOSS_PRICE: {e.initial_stop_loss_price}")
            lines.append(f"     THESIS / CATALYST:       {e.thesis_catalyst}")
            lines.append("")

    # 3. PARTIAL TAKE-PROFIT
    lines.append("-" * 80)
    lines.append(f"🟡 PARTIAL TAKE-PROFIT ({len(partial_tps)}) — +25% Reached -> Sell 50% & Activate Runner Trailing Stop")
    lines.append("-" * 80)
    if not partial_tps:
        lines.append("  (Ei osittaisia voitonkotiutuksia)")
    else:
        for idx, p in enumerate(partial_tps, 1):
            lines.append(f"  {idx}. TICKER:                  {p.ticker}")
            lines.append(f"     SUB_STRATEGY:            {p.sub_strategy}")
            lines.append(f"     ORDER_TYPE:              {p.order_type}")
            lines.append(f"     EXACT_LIMIT_PRICE:       {p.formatted_limit_price()}")
            lines.append(f"     SHARE_COUNT:             {p.share_count} (50% of holding)")
            lines.append(f"     ESTIMATED_TOTAL_EUR:     {p.formatted_estimated_eur()}")
            lines.append(f"     INITIAL_STOP_LOSS_PRICE: {p.initial_stop_loss_price}")
            lines.append(f"     THESIS / CATALYST:       {p.thesis_catalyst}")
            lines.append("")

    lines.append("=" * 80)
    lines.append("Huomautus: Toteuta yllä olevat toimeksiannot manuaalisesti Nordnet OST -tilille ennen pörssiavausta.")
    lines.append("=" * 80)
    return "\n".join(lines)


def format_eod_action_list_html(
    summary: EODPortfolioSummary,
    buys: List[EODActionOrder],
    full_exits: List[EODActionOrder],
    partial_tps: List[EODActionOrder],
) -> str:
    """Formats the executive, modern HTML version of the EOD Action List email."""

    def render_order_table(orders: List[EODActionOrder], badge_color: str, category_name: str) -> str:
        if not orders:
            return f'<p style="color:#64748b;font-style:italic;margin:8px 0 20px 0;">(Ei {category_name.lower()} -toimenpiteitä)</p>'

        rows = ""
        for o in orders:
            rows += f"""
            <tr style="border-bottom:1px solid #e2e8f0;">
              <td style="padding:10px 12px;font-weight:700;color:#0f172a;font-size:1.05rem;">{o.ticker}</td>
              <td style="padding:10px 12px;font-size:0.85rem;color:#475569;"><code>{o.sub_strategy}</code></td>
              <td style="padding:10px 12px;font-size:0.85rem;"><span style="background:{badge_color};color:#ffffff;padding:2px 8px;border-radius:4px;font-weight:600;">{o.order_type}</span></td>
              <td style="padding:10px 12px;font-weight:700;color:#0f172a;text-align:right;">{o.formatted_limit_price()}</td>
              <td style="padding:10px 12px;text-align:right;font-weight:600;">{o.share_count:,}</td>
              <td style="padding:10px 12px;text-align:right;color:#0f172a;font-weight:600;">{o.formatted_estimated_eur()}</td>
              <td style="padding:10px 12px;font-size:0.85rem;color:#dc2626;font-weight:500;">{o.initial_stop_loss_price}</td>
              <td style="padding:10px 12px;font-size:0.85rem;color:#334155;">{o.thesis_catalyst}</td>
            </tr>
            """

        return f"""
        <table style="width:100%;border-collapse:collapse;margin-bottom:24px;background:#ffffff;border-radius:6px;overflow:hidden;border:1px solid #e2e8f0;font-size:0.9rem;">
          <thead>
            <tr style="background:#f8fafc;color:#475569;text-align:left;font-size:0.8rem;text-transform:uppercase;border-bottom:1px solid #cbd5e1;">
              <th style="padding:8px 12px;">Ticker</th>
              <th style="padding:8px 12px;">Sub-Strategy</th>
              <th style="padding:8px 12px;">Order Type</th>
              <th style="padding:8px 12px;text-align:right;">Limit Price</th>
              <th style="padding:8px 12px;text-align:right;">Shares</th>
              <th style="padding:8px 12px;text-align:right;">Est. EUR</th>
              <th style="padding:8px 12px;">Initial Stop</th>
              <th style="padding:8px 12px;">Thesis / Catalyst</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
        """

    hodl_line = ""
    if summary.hodl_positions:
        hodl_items = "".join(
            f'<li style="margin-bottom:4px;"><strong>{p.get("Ticker")}</strong>: {p.get("Shares", 0):,} kpl @ {p.get("Buy Price", 0.0):.2f} € &mdash; <span style="color:#16a34a;font-weight:600;">Ignored by bot / HODL Protected</span></li>'
            for p in summary.hodl_positions
        )
        hodl_line = f"""
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:12px 16px;margin-top:12px;">
          <div style="font-weight:700;color:#166534;margin-bottom:6px;">🔒 HODL Protected Positions (Excluded from Bot Execution):</div>
          <ul style="margin:0;padding-left:20px;color:#15803d;font-size:0.88rem;">
            {hodl_items}
          </ul>
        </div>
        """
    else:
        hodl_line = """
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:8px 14px;margin-top:10px;font-size:0.85rem;color:#64748b;">
          🔒 <strong>HODL Protected Positions:</strong> Ei erillisiä lottolappuja (esim. <code>FARON.HE</code> suojataan automaattisesti lisättäessä).
        </div>
        """

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1e293b;max-width:860px;margin:0 auto;padding:16px;background:#f8fafc;">
  <!-- Header Card -->
  <div style="background:#0f172a;padding:20px 24px;border-radius:8px 8px 0 0;color:#f8fafc;">
    <h2 style="margin:0;font-size:1.4rem;display:flex;align-items:center;">
      🐱 tradeBotTiuku &mdash; EOD Action List: {summary.portfolio_id}
    </h2>
    <p style="margin:6px 0 0 0;font-size:0.85rem;color:#94a3b8;">
      Päivämäärä: {summary.timestamp} &bull; Nordnet OST Execution
    </p>
  </div>

  <div style="border:1px solid #e2e8f0;border-top:none;padding:24px;border-radius:0 0 8px 8px;background:#ffffff;">
    <!-- Portfolio Status Box -->
    <div style="background:#f1f5f9;border:1px solid #cbd5e1;border-radius:6px;padding:14px 18px;margin-bottom:24px;">
      <h4 style="margin:0 0 10px 0;color:#0f172a;font-size:0.95rem;text-transform:uppercase;letter-spacing:0.5px;">
        🏛️ Portfolio Header &amp; Status
      </h4>
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));gap:12px;">
        <div><span style="color:#64748b;font-size:0.85rem;">Regime Status:</span><br><strong style="color:#0f172a;">{summary.regime_status}</strong></div>
        <div><span style="color:#64748b;font-size:0.85rem;">Available Cash:</span><br><strong style="color:#16a34a;font-size:1.15rem;">{summary.available_cash_eur:,.2f} €</strong></div>
        <div><span style="color:#64748b;font-size:0.85rem;">Total P13 Equity (ex-HODL):</span><br><strong style="color:#0f172a;font-size:1.15rem;">{summary.total_equity_eur:,.2f} €</strong></div>
      </div>
      {hodl_line}
    </div>

    <!-- Category 1: BUYS -->
    <h3 style="margin-top:20px;margin-bottom:8px;color:#16a34a;font-size:1.15rem;display:flex;align-items:center;">
      🟢 BUYS ({len(buys)}) &mdash; Syötä LIMIT-ostotoimeksiannot ennen markkina-avausta
    </h3>
    {render_order_table(buys, "#16a34a", "Osto")}

    <!-- Category 2: FULL EXITS -->
    <h3 style="margin-top:24px;margin-bottom:8px;color:#dc2626;font-size:1.15rem;display:flex;align-items:center;">
      🔴 FULL EXITS ({len(full_exits)}) &mdash; Stop-Loss / Fundamental Breakdown / Time-Stop
    </h3>
    {render_order_table(full_exits, "#dc2626", "Myynti")}

    <!-- Category 3: PARTIAL TAKE-PROFIT -->
    <h3 style="margin-top:24px;margin-bottom:8px;color:#d97706;font-size:1.15rem;display:flex;align-items:center;">
      🟡 PARTIAL TAKE-PROFIT ({len(partial_tps)}) &mdash; +25% Saavutettu &rarr; Myy 50% &amp; Aktivoi Runner Trailing Stop
    </h3>
    {render_order_table(partial_tps, "#d97706", "Osittainen Myynti")}

    <div style="margin-top:26px;border-top:1px solid #e2e8f0;padding-top:14px;font-size:0.8rem;color:#64748b;line-height:1.4;">
      <strong>⚠️ Human-In-The-Loop Execution:</strong> Kaikki toimeksiannot suositellaan syötettäväksi Nordnetin OST-tilille ennen pörssiavausta annetuilla LIMIT-kursseilla. US-osakkeille huomioitu 0% osinkovaatimus ja 0.50% FX-vaihtokulu.
    </div>
  </div>
</body>
</html>
"""
    return html


def generate_eod_action_list_email(
    portfolio: Any,
    run_trades: Optional[List[Dict[str, Any]]] = None,
    regime_data: Optional[Dict[str, Any]] = None,
    fx_rates: Optional[Dict[str, float]] = None,
    limit_buffer_pct: float = 0.002,
    prices_cache: Optional[Dict[str, float]] = None,
) -> Tuple[str, str, str]:
    """
    Main generator function for the EOD Action List email.
    Returns:
        (subject, plain_text, html_text)
    """
    summary = build_portfolio_summary(
        portfolio=portfolio,
        regime_data=regime_data,
        fx_rates=fx_rates,
        prices_cache=prices_cache,
    )
    buys, full_exits, partial_tps = build_eod_action_orders(
        portfolio=portfolio,
        run_trades=run_trades,
        fx_rates=fx_rates,
        limit_buffer_pct=limit_buffer_pct,
    )

    now_date = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    total_actions = len(buys) + len(full_exits) + len(partial_tps)
    subject = f"🔔 [OST Action List] {summary.portfolio_id} ({now_date}): {total_actions} toimenpidettä ({len(buys)} ostoa, {len(full_exits)} exit, {len(partial_tps)} TP)"

    text_body = format_eod_action_list_text(summary, buys, full_exits, partial_tps)
    html_body = format_eod_action_list_html(summary, buys, full_exits, partial_tps)

    return subject, text_body, html_body


def send_eod_action_list_email(
    subject: str,
    text_body: str,
    html_body: str,
    alert_email: Optional[str] = None,
) -> bool:
    """
    Transmits the formatted EOD action list via SMTP. Fails gracefully if SMTP is offline.
    """
    creds = load_env_credentials()
    recipient = alert_email or creds.get("ALERT_EMAIL")
    if not recipient:
        logger.warning("No ALERT_EMAIL recipient configured. EOD action email skipped.")
        return False

    smtp_server = creds.get("SMTP_SERVER", "")
    smtp_port = int(creds.get("SMTP_PORT", 587) or 587)
    smtp_user = creds.get("SMTP_USER", "")
    smtp_pass = creds.get("SMTP_PASS", "")

    if not smtp_server:
        logger.info("No SMTP_SERVER configured. EOD action list logged to console only.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user or "tradebot@localhost"
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        logger.info(f"Sending EOD Action List email to {recipient} via {smtp_server}:{smtp_port}...")
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()

        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)

        server.sendmail(msg["From"], [recipient], msg.as_string())
        server.quit()
        logger.info(f"Successfully sent EOD Action List email: {subject}")
        return True
    except Exception as e:
        logger.warning(f"Failed to send EOD Action List email: {e}. Gracefully continuing.")
        return False


def generate_and_send_p13_eod_action_email(
    portfolio: Any,
    daemon: Any,
    run_trades: Optional[List[Dict[str, Any]]] = None,
    send_if_empty: bool = False,
) -> bool:
    """Convenience integration method invoked by main_controller at EOD."""
    regime = getattr(daemon.market_engine, "market_regime", {}) if hasattr(daemon, "market_engine") else {}
    fx = getattr(daemon.market_engine, "fx_rates", None) if hasattr(daemon, "market_engine") else None
    limit_buf = getattr(portfolio.config, "limit_price_buffer_pct", 0.002) if hasattr(portfolio, "config") else 0.002
    if limit_buf > 0.05:  # e.g. if passed as 0.2 meaning 0.2%
        limit_buf = limit_buf / 100.0

    buys, full_exits, partial_tps = build_eod_action_orders(
        portfolio=portfolio,
        run_trades=run_trades,
        fx_rates=fx,
        limit_buffer_pct=limit_buf,
    )
    total_actions = len(buys) + len(full_exits) + len(partial_tps)

    px_cache = getattr(daemon.market_engine, "prices_cache", {}) if hasattr(daemon, "market_engine") else {}

    subject, text_body, html_body = generate_eod_action_list_email(
        portfolio=portfolio,
        run_trades=run_trades,
        regime_data=regime,
        fx_rates=fx,
        limit_buffer_pct=limit_buf,
        prices_cache=px_cache,
    )

    # Always log text version for local inspection
    logger.info(f"\n{text_body}")

    if total_actions == 0 and not send_if_empty:
        logger.info(
            f"[{getattr(portfolio.config, 'portfolio_id', 'P13')}] "
            f"0 toimenpidettä Action Listissä. Sähköpostia ei lähetetä (hiljainen tila)."
        )
        return False

    return send_eod_action_list_email(subject, text_body, html_body)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    import argparse
    parser = argparse.ArgumentParser(description="Generate and test EOD Action List Email.")
    parser.add_argument("--send", action="store_true", help="Send actual email via SMTP")
    args = parser.parse_args()

    class DummyConfig:
        portfolio_id = "P13_Live_Candidate"
        limit_price_buffer_pct = 0.2

    class DummyPortfolio:
        config = DummyConfig()
        cash_balance = 7350.0
        positions = [
            {"Ticker": "FARON.HE", "Shares": 1000, "Buy Price": 2.10, "hodl": True, "Currency": "EUR"},
            {"Ticker": "QTCOM.HE", "Shares": 35, "Buy Price": 75.0, "hodl": False, "Currency": "EUR"},
        ]

    dummy_trades = [
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "BUY",
            "ticker": "HARVIA.HE",
            "details": {
                "sub_strategy": "core_nordic_midcap_quality",
                "buy_price": 38.50,
                "shares": 60,
                "currency": "EUR",
                "thesis": "Nordic MidCap Compounder: ROE 24.5%, EBIT 18.2%, FCF 32M€, NetDebt/EBITDA 1.1",
            },
        },
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "SELL",
            "ticker": "LOSS_CO.ST",
            "details": {
                "sub_strategy": "satellite_nordic_microcap_catalyst",
                "sell_price": 14.20,
                "shares": 300,
                "currency": "SEK",
                "exit_reason": "STOP_LOSS_SATELLITE (Loss -12.4% >= -12.0%)",
            },
        },
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "PARTIAL_TP",
            "ticker": "GROWTH.US",
            "details": {
                "sub_strategy": "satellite_us_microcap_growth",
                "sell_price": 25.00,
                "shares": 50,
                "currency": "USD",
                "entry_price": 20.00,
                "exit_reason": "+25.0% Reached -> Sell 50% & Lock Profit; Activate Runner Trailing Stop",
            },
        },
    ]

    regime = {"regime": "🟢 BULL / LOW VOL", "last_price": 115.4, "sma_50": 112.0}
    subj, txt, htm = generate_eod_action_list_email(DummyPortfolio(), dummy_trades, regime)

    print("\n" + "="*80)
    print(f"SUBJECT: {subj}")
    print("="*80)
    print(txt)

    if args.send:
        send_eod_action_list_email(subj, txt, htm)
