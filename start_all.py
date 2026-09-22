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
        "--interval-hours",
        type=float,
        default=4.0,
        help="Papertrader evaluation interval in hours (default: 4.0)",
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

    args = parser.parse_args()

    processes: list[tuple[str, subprocess.Popen]] = []

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
            processes.append(("Streamlit UI Dashboard", p_ui))

        # 2. Start Paper Trader Daemon
        if not args.ui_only:
            mode_desc = "kerran (--run-once)" if args.run_once else f"silmukassa ({args.interval_hours}h välein)"
            print(f"🤖 [2/2] Käynnistetään Paper Trader Daemon ({mode_desc})...")
            daemon_cmd = [sys.executable, str(BASE_DIR / "main_controller.py")]
            if args.run_once:
                daemon_cmd.append("--run-once")
            else:
                daemon_cmd.extend(["--loop", "--interval-hours", str(args.interval_hours)])

            p_daemon = subprocess.Popen(daemon_cmd, cwd=str(BASE_DIR))
            processes.append(("Paper Trader Daemon", p_daemon))

        print("\n" + "=" * 65)
        print("✅ tradeBotTiuku käynnistetty onnistuneesti!")
        if not args.daemon_only:
            print(f"📊 UI Dashboard osoitteessa: http://localhost:{args.port}")
        print("Paina Ctrl+C sammuttaaksesi molemmat prosessit siististi.")
        print("=" * 65 + "\n")

        # Monitor loop
        while True:
            time.sleep(1)
            for name, p in processes:
                ret = p.poll()
                if ret is not None:
                    print(f"⚠️ Prosessi '{name}' päättyi koodilla {ret}.")
                    return

    except KeyboardInterrupt:
        print("\n🛑 Sammutetaan tradeBotTiuku -prosessit...")
    finally:
        for name, p in processes:
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
