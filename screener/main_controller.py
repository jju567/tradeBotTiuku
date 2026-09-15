"""
Main Orchestration Controller for Nasdaq OMX Helsinki Micro-Cap Stock Screener.

Ties together:
1. `scraper_module.py` (RSS feeds & in-memory PDF extraction)
2. `state_manager.py` (SQLite state tracking & deduplication)
3. `nlp_analyzer.py` (OpenRouter LLM text analysis with rate limiting)

Pipeline Execution Steps:
Step 1: Fetch latest releases from Nasdaq CDS, First North, GlobeNewswire, and Cision.
Step 2: Iterate through fetched releases.
Step 3: Check `state_manager.is_processed(article_id)` (skip if True).
Step 4: Extract HTML/PDF text and call `nlp_analyzer.analyze_text()`.
Step 5: Evaluate signals:
        - If `cash_issue` == True -> Reject immediately (Cash Risk).
        - If `management_buying` == True OR `positive_guidance` == True -> Strong Buy Signal.
Step 6: Call `state_manager.mark_as_processed(article_id, title, date)`.
Step 7: Append candidates to `data/screener_alerts.csv` and log formatted alerts.
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# Load .env variables
load_dotenv()

# Reconfigure stdout/stderr for cross-platform encoding compatibility
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure parent directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from screener.config import ScreenerConfig
from screener.models import FeedItem
from screener.scraper_module import NasdaqHelsinkiScraper, fetch_latest_releases
from screener.state_manager import init_db, is_processed, mark_as_processed
from screener.nlp_analyzer import analyze_text, FinancialNLPAnalyzer
from screener.quant_engine import QuantitativeRiskEngine, check_liquidity_and_spread

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("screener.main_controller")

DEFAULT_ALERTS_CSV = BASE_DIR / "data" / "screener_alerts.csv"


class ScreenerPipelineController:
    """Orchestrates ingestion, state management, NLP evaluation, and alerting."""

    def __init__(
        self,
        config: Optional[ScreenerConfig] = None,
        alerts_csv_path: Optional[Path | str] = None,
        openrouter_api_key: Optional[str] = None,
    ):
        self.config = config or ScreenerConfig.from_env()
        self.alerts_csv_path = Path(alerts_csv_path or DEFAULT_ALERTS_CSV)
        self.alerts_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_key = openrouter_api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")

        # 1. Initialize SQLite State Database
        init_db(self.config.db_path)
        logger.info(f"State database initialized at {self.config.db_path}")

        # 2. Check API key status
        if not self.api_key:
            logger.warning(
                "OPENROUTER_API_KEY is not set in environment or .env file. "
                "The pipeline will use the built-in deterministic Finnish keyword NLP engine."
            )
        else:
            logger.info("OpenRouter API key detected and loaded.")

        self.scraper = NasdaqHelsinkiScraper(self.config)
        self.quant_engine = QuantitativeRiskEngine(self.config)

    def run_pipeline_cycle(self, dry_run: bool = False) -> List[Dict[str, Any]]:
        """
        Runs one complete pass of the screening pipeline.
        Returns the list of strong buy candidates found in this cycle.
        """
        logger.info("=" * 70)
        logger.info(">>> STARTING SCREENING CYCLE (Nasdaq Helsinki Small Cap & First North) <<<")
        logger.info("=" * 70)
        start_time = time.time()

        # Step 1: Fetch latest releases
        logger.info("Step 1: Fetching latest releases from exchange feeds...")
        try:
            releases: List[FeedItem] = fetch_latest_releases(self.config)
        except Exception as e:
            logger.error(f"Failed to fetch releases from feeds: {e}")
            return []

        if not releases:
            logger.info("No releases returned from feeds. Cycle complete.")
            return []

        logger.info(f"Retrieved {len(releases)} total feed items.")
        
        candidates_found: List[Dict[str, Any]] = []
        processed_count = 0
        skipped_count = 0

        # Step 2: Iterate through fetched releases
        for idx, release in enumerate(releases, start=1):
            article_id = release.guid or release.link
            title = release.title
            pub_date = release.published_at.isoformat() if release.published_at else ""

            # Step 3: Check state database (Skip already processed)
            if is_processed(article_id, self.config.db_path):
                skipped_count += 1
                continue

            processed_count += 1
            logger.info(f"[{processed_count}] Processing new release: {title[:75]}...")

            # Step 4: Extract text & NLP analysis
            try:
                # Fetch full document (HTML content and in-memory attached PDFs)
                doc = self.scraper.fetch_and_parse_document(release.link)
                
                # Combine headers and full text for LLM analysis
                header_info = f"{title}\n{release.category or ''}\n{release.summary or ''}"
                full_text = f"{header_info}\n\n{doc.full_combined_text}".strip()

                if not full_text:
                    logger.warning(f"No text content could be extracted for {release.link}. Marking processed.")
                    if not dry_run:
                        mark_as_processed(article_id, title, pub_date, self.config.db_path)
                    continue

                # Pass text to OpenRouter NLP analyzer
                signals = analyze_text(
                    text_content=full_text,
                    api_key=self.api_key,
                )

            except Exception as e:
                # If LLM or network fails with unhandled error, do NOT mark processed so bot retries next time
                logger.error(f"Error processing release {article_id}: {e}. Will retry on next cycle.")
                continue

            # Step 5: Evaluate signals
            cash_issue = signals.get("cash_issue", False)
            insider_buying_personal = signals.get("insider_buying_personal", signals.get("management_buying", False))
            company_share_buyback = signals.get("company_share_buyback", False)
            pos_guidance = signals.get("positive_guidance", False)

            if cash_issue:
                # Immediate Reject: Cash Risk
                logger.info(f"[-] Rejected (Cash Risk / Funding Distress): {title[:60]}")
            elif company_share_buyback and not insider_buying_personal and not pos_guidance:
                # Company Repurchasing Own Shares (Informational only, not a strong buy trigger)
                logger.info(f"[INFO] Company Share Buyback program (No insider personal buy): {title[:65]}")
            elif insider_buying_personal or pos_guidance:
                # Strong Buy Signal Detected by LLM
                signal_types = []
                if insider_buying_personal:
                    signal_types.append("INSIDER_BUYING")
                if pos_guidance:
                    signal_types.append("POSITIVE_GUIDANCE")
                
                signal_label = " & ".join(signal_types)
                logger.info(f"[+] LLM Detected Strong Buy Signal ({signal_label}): {title}")

                # Step 5b: Quantitative Liquidity & Spread Filter (Gatekeeper)
                ticker = release.ticker
                spread_pct = None
                bid = None
                ask = None
                volume = 0

                if ticker:
                    liquidity_res = check_liquidity_and_spread(
                        ticker=ticker,
                        max_spread_pct=self.config.max_bid_ask_spread_pct,
                        min_volume=2000,
                        commission_min_eur=self.config.nordnet_min_commission_eur,
                        commission_percent=self.config.nordnet_commission_percent,
                        sample_trade_size_eur=self.config.nordnet_min_trade_eur * 2.0,
                        max_total_friction_pct=self.config.max_total_friction_pct,
                    )
                    spread_pct = liquidity_res.get("spread_pct")
                    bid = liquidity_res.get("bid")
                    ask = liquidity_res.get("ask")
                    volume = liquidity_res.get("volume", 0)
                    total_friction_pct = liquidity_res.get("total_friction_pct")
                    comm_round_trip = liquidity_res.get("commission_round_trip_eur", 14.0)
                    min_trade = liquidity_res.get("min_recommended_trade_eur", self.config.nordnet_min_trade_eur)

                    if not liquidity_res["passed_spread_check"]:
                        logger.info(
                            f"[-] Rejected by Quant Liquidity & Nordnet Friction Filter ({ticker}): {liquidity_res['reason']}"
                        )
                        # Mark processed and skip alerting
                        if not dry_run:
                            mark_as_processed(article_id, title, pub_date, self.config.db_path)
                        continue
                    else:
                        logger.info(
                            f"[+] Passed Liquidity & Friction Check for {ticker}: Spread={spread_pct}%, Friction={total_friction_pct}%, Vol={volume}"
                        )
                else:
                    total_friction_pct = None
                    comm_round_trip = 14.0
                    min_trade = self.config.nordnet_min_trade_eur
                    logger.warning(
                        f"No ticker resolved for release '{title[:50]}'. Skipping spread filter and proceeding."
                    )

                candidate = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "company_name": release.company_name or "Unknown",
                    "ticker": ticker or "",
                    "title": title,
                    "link": release.link,
                    "signals": signal_label,
                    "insider_buying_personal": insider_buying_personal,
                    "company_share_buyback": company_share_buyback,
                    "management_buying": insider_buying_personal,
                    "positive_guidance": pos_guidance,
                    "cash_issue": cash_issue,
                    "spread_pct": spread_pct,
                    "bid": bid,
                    "ask": ask,
                    "volume": volume,
                    "total_friction_pct": total_friction_pct,
                    "commission_round_trip_eur": comm_round_trip,
                    "min_recommended_trade_eur": min_trade,
                    "pdf_attached": len(doc.pdf_urls) > 0,
                }
                candidates_found.append(candidate)

                # Output alert to console
                self._print_alert(candidate)

                # Output alert to CSV
                if not dry_run:
                    self._append_to_csv(candidate)
            else:
                logger.debug(f"[.] Neutral / No Actionable Signal: {title[:60]}")

            # Step 6: Commit state in SQLite (never analyze again)
            if not dry_run:
                mark_as_processed(article_id, title, pub_date, self.config.db_path)

        elapsed = time.time() - start_time
        logger.info("=" * 70)
        logger.info(
            f"Screening cycle finished in {elapsed:.2f}s. "
            f"({skipped_count} skipped, {processed_count} new evaluated, {len(candidates_found)} alerts)"
        )
        logger.info("=" * 70)

        return candidates_found

    def _print_alert(self, candidate: Dict[str, Any]) -> None:
        """Print highlighted alert to console."""
        spread_info = (
            f"{candidate['spread_pct']}% (Bid: {candidate['bid']} / Ask: {candidate['ask']})"
            if candidate.get("spread_pct") is not None
            else "N/A"
        )
        friction_info = (
            f"{candidate['total_friction_pct']}% (Nordnet edestakainen kulu: {candidate.get('commission_round_trip_eur')} EUR)"
            if candidate.get("total_friction_pct") is not None
            else "N/A"
        )
        print("\n" + "*" * 80)
        print(f"[ALERT] STRONG BUY SIGNAL: {candidate['company_name']} ({candidate['ticker'] or 'N/A'})")
        print(f"Signals:        {candidate['signals']}")
        print(f"Spread:         {spread_info} | Vol: {candidate.get('volume', 'N/A')}")
        print(f"Total Friction: {friction_info} | Min. ostosuositus: >= {candidate.get('min_recommended_trade_eur', 500):.0f} EUR")
        print(f"Title:          {candidate['title']}")
        print(f"Link:           {candidate['link']}")
        print(f"PDF Report:     {'Yes' if candidate['pdf_attached'] else 'No'}")
        print("*" * 80 + "\n")

    def _append_to_csv(self, candidate: Dict[str, Any]) -> None:
        """Append candidate record to alerts CSV file."""
        file_exists = self.alerts_csv_path.exists()
        try:
            with open(self.alerts_csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "timestamp", "company_name", "ticker", "title",
                        "link", "signals", "spread_pct", "bid", "ask", "volume",
                        "total_friction_pct", "commission_round_trip_eur", "min_recommended_trade_eur",
                        "management_buying", "positive_guidance", "pdf_attached"
                    ])
                writer.writerow([
                    candidate["timestamp"],
                    candidate["company_name"],
                    candidate["ticker"],
                    candidate["title"],
                    candidate["link"],
                    candidate["signals"],
                    candidate.get("spread_pct", ""),
                    candidate.get("bid", ""),
                    candidate.get("ask", ""),
                    candidate.get("volume", ""),
                    candidate.get("total_friction_pct", ""),
                    candidate.get("commission_round_trip_eur", ""),
                    candidate.get("min_recommended_trade_eur", ""),
                    candidate["management_buying"],
                    candidate["positive_guidance"],
                    candidate["pdf_attached"],
                ])
        except Exception as e:
            logger.error(f"Failed to append alert to {self.alerts_csv_path}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Nasdaq OMX Helsinki Micro-Cap Stock Screener Orchestrator"
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run the screening pipeline once and exit (ideal for testing or cron jobs)"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run the pipeline continuously in a scheduled loop"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Polling interval in seconds when running in --loop mode (default: 3600 = 1 hour)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute the pipeline without persisting to SQLite or appending to CSV"
    )
    args = parser.parse_args()

    controller = ScreenerPipelineController()

    if args.loop:
        logger.info(f"Starting continuous screening loop (interval: {args.interval} seconds / {args.interval/60:.1f} minutes)...")
        try:
            while True:
                controller.run_pipeline_cycle(dry_run=args.dry_run)
                logger.info(f"Sleeping for {args.interval} seconds until next cycle...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Screening loop terminated by user (KeyboardInterrupt). Exiting cleanly.")
    else:
        # Default mode: run once
        controller.run_pipeline_cycle(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
