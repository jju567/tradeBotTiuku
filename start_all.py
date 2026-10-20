"""
start_all.py - Unified Launcher for tradeBotTiuku UI & Paper Trader Daemon.

Launches both:
  1. Streamlit Web Dashboard (dashboard.py)
  2. Paper Trading Live Daemon (main_controller.py)
With clean Ctrl+C graceful shutdown.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def main() -> None:
    parser = argparse.ArgumentParser(
        description="tradeBotTiuku — Launch UI Dashboard and Paper Trader Daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run Paper Trader Daemon in legacy interval loop instead of daily scheduler",
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=4.0,
        help="Papertrader evaluation interval in hours when --loop is used (default: 4.0)",
    )
    parser.add_argument(
        "--ui-only",
        action="store_true",
        help="Start only Streamlit UI Dashboard",
    )
    parser.add_argument(
        "--daemon-only",
        action="store_true",
        help="Start only Paper Trader Daemon",
    )
    default_port = int(os.getenv("STREAMLIT_PORT", "8502"))
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help=f"Streamlit UI web server port (default: {default_port})",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run a single papertrader cycle instead of continuous loop",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        default=True,
        help="Run Paper Trader Daemon with in-code scheduler at daily specified time (default: enabled)",
    )
    parser.add_argument(
        "--daily-at",
        type=str,
        default="23:05",
        help="Daily execution time for scheduler in HH:MM format (default: 23:05)",
    )
    parser.add_argument(
        "--intraday-p12-minutes",
        type=int,
        default=30,
        help="DEPRECATED: Intraday scanning decommissioned. Analysis operates purely End-Of-Day (EOD)",
    )
    parser.add_argument(
        "--sync-universe",
        action="store_true",
        help="Force an immediate dynamic universe scan for new listings from Nordnet",
    )

    args = parser.parse_args()

    daemon_cmd: list[str] = []
    p_ui: subprocess.Popen | None = None
    p_daemon: subprocess.Popen | None = None

    try:
        # 1. Start Streamlit UI Dashboard
        if not args.daemon_only:
            print("🚀 [1/2] Käynnistetään Streamlit UI (dashboard.py)...")
            ui_cmd = [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(BASE_DIR / "dashboard.py"),
                "--server.port",
                str(args.port),
            ]
            p_ui = subprocess.Popen(ui_cmd, cwd=str(BASE_DIR))

        # 2. Build Paper Trader Daemon command
        if not args.ui_only:
            if args.run_once:
                mode_desc = "kerran (--run-once)"
            elif args.loop:
                mode_desc = f"silmukassa ({args.interval_hours}h välein)"
            else:
                mode_desc = f"koodiajastimella (iltasykli klo {args.daily_at})"
            print(f"🤖 [2/2] Käynnistetään Paper Trader Daemon ({mode_desc})...")
            daemon_cmd = [sys.executable, str(BASE_DIR / "main_controller.py")]
            if args.sync_universe:
                daemon_cmd.append("--sync-universe")
            if args.run_once:
                daemon_cmd.append("--run-once")
            elif args.loop:
                daemon_cmd.extend(["--loop", "--interval-hours", str(args.interval_hours)])
            else:
                daemon_cmd.extend([
                    "--schedule",
                    "--daily-at", str(args.daily_at),
                ])

            p_daemon = subprocess.Popen(daemon_cmd, cwd=str(BASE_DIR))

        print("\n" + "=" * 65)
        print("✅ tradeBotTiuku käynnistetty onnistuneesti!")
        if not args.daemon_only:
            print(f"📊 UI Dashboard osoitteessa: http://localhost:{args.port}")
        print("Paina Ctrl+C sammuttaaksesi molemmat prosessit siististi.")
        print("=" * 65 + "\n")

        # Monitor loop with resilient daemon auto-respawn
        shutdown_initiated = False
        while not shutdown_initiated:
            time.sleep(1)

            # Check UI process
            if p_ui is not None:
                ret_ui = p_ui.poll()
                if ret_ui is not None:
                    print(f"⚠️ Streamlit UI päättyi koodilla {ret_ui}. Suljetaan järjestelmä...")
                    break

            # Check Daemon process
            if p_daemon is not None and not args.run_once:
                ret_d = p_daemon.poll()
                if ret_d is not None:
                    print(f"🔄 Paper Trader Daemon päättyi (koodi {ret_d}). Käynnistetään automaattisesti uudelleen...")
                    time.sleep(2)
                    p_daemon = subprocess.Popen(daemon_cmd, cwd=str(BASE_DIR))
                    print(f"✅ Paper Trader Daemon uudelleenkäynnistetty (PID: {p_daemon.pid}).")
            elif p_daemon is not None and args.run_once:
                ret_d = p_daemon.poll()
                if ret_d is not None:
                    print(f"✅ Paper Trader Daemon suoritti kerta-ajon (--run-once).")
                    p_daemon = None

    except KeyboardInterrupt:
        print("\n🛑 Sammutetaan tradeBotTiuku -prosessit...")
    finally:
        for p in [p_ui, p_daemon]:
            if p is not None:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        print("👋 Kaikki prosessit on suljettu.")

if __name__ == "__main__":
    main()
