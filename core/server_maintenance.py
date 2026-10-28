"""
core/server_maintenance.py - Server Remote Maintenance & Process Lifecycle Manager.

Provides robust utilities to:
1. Inspect Git repository state (current commit, branch, dirty status, remote).
2. Execute 'git pull' safely without wiping local data files (using --autostash).
3. Monitor daemon process liveness (PID, heartbeat, uptime, systemd status).
4. Restart the Paper Trader Daemon cleanly across Linux (systemd / pgrep / SIGTERM)
   and Windows (process termination + background respawn).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Configure cross-platform terminal encoding (Windows cp1252 safety)
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

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DAEMON_STATUS_JSON = DATA_DIR / "daemon_status.json"


def _atomic_write_json(target_path: Path, payload: Any, ensure_ascii: bool = False) -> None:
    """Writes JSON atomically via a PID-scoped temporary file and os.replace."""
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=ensure_ascii)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        for attempt in range(5):
            try:
                os.replace(tmp_path, target_path)
                break
            except Exception:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def _run_cmd(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 45) -> Tuple[int, str, str]:
    """Runs a shell command synchronously and returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Komento aikakatkaistiin ({timeout}s): {' '.join(cmd)}"
    except Exception as e:
        return -1, "", str(e)


def get_git_info() -> Dict[str, Any]:
    """
    Retrieves current Git status: branch, short hash, commit message, date, and dirty flag.
    """
    info: Dict[str, Any] = {
        "is_git": False,
        "branch": "unknown",
        "commit_hash": "N/A",
        "commit_msg": "",
        "commit_date": "",
        "is_dirty": False,
        "remote_url": "",
    }

    # Verify if inside a git repository
    rc, out, _ = _run_cmd(["git", "rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.lower() != "true":
        return info

    info["is_git"] = True

    # Current branch
    rc, branch, _ = _run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and branch:
        info["branch"] = branch

    # Short commit hash
    rc, chash, _ = _run_cmd(["git", "rev-parse", "--short", "HEAD"])
    if rc == 0 and chash:
        info["commit_hash"] = chash

    # Commit message & relative/iso date
    rc, cinfo, _ = _run_cmd(["git", "log", "-1", "--format=%s||%ci||%cr"])
    if rc == 0 and cinfo:
        parts = cinfo.split("||")
        if len(parts) >= 1:
            info["commit_msg"] = parts[0].strip()
        if len(parts) >= 2:
            info["commit_date"] = parts[1].strip()
        if len(parts) >= 3:
            info["commit_relative"] = parts[2].strip()

    # Remote URL
    rc, remote, _ = _run_cmd(["git", "config", "--get", "remote.origin.url"])
    if rc == 0 and remote:
        # Mask any embedded credentials
        clean_remote = remote
        if "@" in remote and "://" in remote:
            prefix = remote.split("://")[0]
            host_path = remote.split("@")[-1]
            clean_remote = f"{prefix}://***@{host_path}"
        info["remote_url"] = clean_remote

    # Dirty status (untracked files excluded, checking modified tracked files)
    rc, status_out, _ = _run_cmd(["git", "status", "--porcelain"])
    if rc == 0:
        lines = [line for line in status_out.splitlines() if not line.startswith("??")]
        info["is_dirty"] = len(lines) > 0
        info["dirty_files_count"] = len(lines)

    return info


def is_pid_alive(pid: int) -> bool:
    """Checks if a process with given PID is currently active."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False


def find_daemon_pids() -> List[int]:
    """
    Finds PIDs of all running `main_controller.py` processes.
    Uses psutil if available, otherwise OS-specific commands.
    """
    pids: List[int] = []

    # Try psutil first if installed
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline", "name"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                cmd_str = " ".join(cmdline).lower()
                if "main_controller.py" in cmd_str and not "test" in cmd_str:
                    p = proc.info.get("pid")
                    if p and p != os.getpid() and p not in pids:
                        pids.append(p)
            except Exception:
                continue
        if pids:
            return pids
    except Exception:
        pass

    # Linux fallback: pgrep
    if os.name != "nt":
        rc, out, _ = _run_cmd(["pgrep", "-f", "main_controller.py"])
        if rc == 0 and out:
            for line in out.splitlines():
                try:
                    p = int(line.strip())
                    if p != os.getpid() and p not in pids:
                        pids.append(p)
                except ValueError:
                    pass
        return pids

    # Windows fallback: WMIC / PowerShell Get-CimInstance
    if os.name == "nt":
        rc, out, _ = _run_cmd([
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main_controller.py*' -and $_.ProcessId -ne "
            + str(os.getpid())
            + " } | Select-Object -ExpandProperty ProcessId",
        ])
        if rc == 0 and out:
            for line in out.splitlines():
                try:
                    p = int(line.strip())
                    if p not in pids:
                        pids.append(p)
                except ValueError:
                    pass

    return pids


def get_daemon_status() -> Dict[str, Any]:
    """
    Returns full status dictionary of the Paper Trader Daemon.
    Combines data from `data/daemon_status.json`, systemd queries, and live PID inspection.
    """
    status: Dict[str, Any] = {
        "is_running": False,
        "pids": [],
        "systemd_service": None,
        "daily_at": "23:05",
        "started_at": None,
        "last_heartbeat": None,
        "status_text": "Pysäytetty / Ei käynnissä",
    }

    # 1. Read state file if present
    file_status = {}
    if DAEMON_STATUS_JSON.exists() and DAEMON_STATUS_JSON.stat().st_size > 0:
        try:
            with open(DAEMON_STATUS_JSON, "r", encoding="utf-8") as f:
                file_status = json.load(f)
            status["daily_at"] = file_status.get("daily_at", "23:05")
            status["started_at"] = file_status.get("started_at")
            status["last_heartbeat"] = file_status.get("last_heartbeat")
        except Exception:
            pass

    # 2. Check systemd on Linux
    if os.name != "nt":
        for svc in ["tradebot-tiuku-daemon", "tradebot-tiuku"]:
            rc, out, _ = _run_cmd(["systemctl", "is-active", svc])
            if rc == 0 and out == "active":
                status["systemd_service"] = svc
                status["is_running"] = True
                break

    # 3. Live PIDs check
    live_pids = find_daemon_pids()
    if not live_pids and file_status.get("pid"):
        f_pid = int(file_status["pid"])
        if is_pid_alive(f_pid):
            live_pids.append(f_pid)

    status["pids"] = live_pids
    if live_pids:
        status["is_running"] = True
        status["status_text"] = f"🟢 Käynnissä (PID: {', '.join(str(p) for p in live_pids)})"
    elif status.get("systemd_service"):
        status["status_text"] = f"🟢 Käynnissä systemd-palveluna ({status['systemd_service']})"
    else:
        status["status_text"] = "🔴 Ei käynnissä"

    return status


def git_pull(branch: Optional[str] = None) -> Tuple[bool, str]:
    """
    Performs a safe git pull from remote.
    Uses --autostash to protect any untracked or locally modified config files on the server.
    If a conflict occurs, automatically performs an abort/rollback so the working directory
    is never left in a broken or merging state, and the running daemon is untouched.
    """
    git_info = get_git_info()
    if not git_info.get("is_git"):
        return False, "Hakemisto ei ole Git-repositorio."

    target_branch = branch or git_info.get("branch") or "main"
    logs: List[str] = []

    # 1. Fetch
    logs.append(f"📡 Noudetaan muutokset kohteesta origin/{target_branch}...")
    rc, out, err = _run_cmd(["git", "fetch", "origin", target_branch])
    if rc != 0:
        return False, f"Git fetch epäonnistui (koodi {rc}):\n{err or out}"

    # 2. Pull with autostash
    logs.append(f"⬇️ Suoritetaan git pull --autostash origin {target_branch}...")
    rc, out, err = _run_cmd(["git", "pull", "--autostash", "origin", target_branch])
    combined_out = f"{out}\n{err}".lower()
    has_stash_conflict = (
        "applying autostash resulted in conflicts" in combined_out
        or "conflict (content)" in combined_out
        or "unmerged paths" in combined_out
    )

    if rc != 0 or has_stash_conflict:
        logs.append(f"❌ Git pull kohtasi ristiriidan (koodi {rc}):\n{err or out}")
        # Automatic Rollback / Clean Sync to prevent broken <<<<<<< Updated upstream syntax errors
        logs.append("🛡️ [Auto-Rollback] Poistetaan konfliktimerkit ja palautetaan kooditiedostot ehjään tilaan...")
        _run_cmd(["git", "merge", "--abort"])
        _run_cmd(["git", "rebase", "--abort"])
        _run_cmd(["git", "reset", "--hard", f"origin/{target_branch}"])
        logs.append("✅ Automaattinen peruutus ja konfliktin siivous suoritettu. Palvelimen koodi ja daemon ovat ehjässä tilassa.")
        if has_stash_conflict:
            new_info = get_git_info()
            logs.append(f"✅ Päivitetty puhtaasti GitHubin tilaan: {new_info.get('commit_hash')} — {new_info.get('commit_msg')}")
            return True, "\n".join(logs)
        return False, "\n".join(logs)

    logs.append(out if out else "Koodi ajan tasalla.")

    # 3. Get new commit hash & subject
    new_info = get_git_info()
    logs.append(f"✅ Uusin commit: {new_info.get('commit_hash')} — {new_info.get('commit_msg')}")

    return True, "\n".join(logs)


def git_hard_reset_to_origin(branch: Optional[str] = None) -> Tuple[bool, str]:
    """
    Emergency override: Forces local branch to match origin/<branch> exactly.
    Safely resolves unrecoverable merge conflicts without touching ignored data/ portfolios.
    """
    git_info = get_git_info()
    if not git_info.get("is_git"):
        return False, "Hakemisto ei ole Git-repositorio."

    target_branch = branch or git_info.get("branch") or "main"
    logs: List[str] = [f"⚠️ Suoritetaan pakotettu synkronointi kohteeseen origin/{target_branch}..."]

    # Abort any in-progress merge/rebase
    _run_cmd(["git", "merge", "--abort"])
    _run_cmd(["git", "rebase", "--abort"])

    # Fetch fresh
    rc, out, err = _run_cmd(["git", "fetch", "origin", target_branch])
    if rc != 0:
        return False, f"Git fetch epäonnistui:\n{err or out}"

    # Reset hard
    rc, out, err = _run_cmd(["git", "reset", "--hard", f"origin/{target_branch}"])
    if rc != 0:
        return False, f"Git reset epäonnistui:\n{err or out}"

    new_info = get_git_info()
    logs.append(f"✅ Palvelin synkronoitu pakotetusti tilaan: {new_info.get('commit_hash')} — {new_info.get('commit_msg')}")
    return True, "\n".join(logs)


def restart_daemon(daily_at: str = "23:05") -> Tuple[bool, str]:
    """
    Restarts the Paper Trader Daemon cleanly.
    Tries systemd first, then process termination (with systemd/supervisor auto-respawn),
    and falls back to spawning a new detached background process if not supervised.
    """
    logs: List[str] = []
    pids = find_daemon_pids()

    # Strategy A: Linux systemctl restart if running as systemd service
    if os.name != "nt":
        for svc in ["tradebot-tiuku-daemon", "tradebot-tiuku"]:
            # Check if service is loaded
            rc, out, _ = _run_cmd(["systemctl", "status", svc])
            if rc == 0 or "Loaded: loaded" in out:
                logs.append(f"🔄 Yritetään käynnistää systemd-palvelu '{svc}' uudelleen...")
                # Try systemctl with sudo -n (non-interactive)
                rc_rst, out_rst, err_rst = _run_cmd(["sudo", "-n", "systemctl", "restart", svc])
                if rc_rst == 0:
                    time.sleep(1.5)
                    st_svc = get_daemon_status()
                    return True, f"✅ Systemd-palvelu '{svc}' käynnistetty uudelleen onnistuneesti!\n{st_svc.get('status_text')}"
                else:
                    logs.append(f"⚠️ 'sudo systemctl restart {svc}' ei onnistunut suoraan ({err_rst or out_rst}). Kokeillaan prosessin siistiä sammutusta...")

    # Strategy B: Terminate existing daemon processes
    if pids:
        logs.append(f"🛑 Sammutetaan vanha daemon-prosessi (PID(s): {', '.join(str(p) for p in pids)})...")
        for pid in pids:
            try:
                if os.name == "nt":
                    _run_cmd(["taskkill", "/F", "/T", "/PID", str(pid)])
                else:
                    os.kill(pid, signal.SIGTERM)
            except Exception as e:
                logs.append(f"⚠️ Varoitus suljettaessa PID {pid}: {e}")

        # Wait up to 3 seconds for supervisor (systemd / start_all.py) to respawn it
        time.sleep(2.0)
        new_pids = find_daemon_pids()
        if new_pids:
            logs.append(f"✅ Valvoja käynnisti daemonin automaattisesti uudella koodilla (Uusi PID: {', '.join(str(p) for p in new_pids)})!")
            return True, "\n".join(logs)

    # Strategy C: Standalone respawn if no supervisor respawned it
    logs.append(f"🚀 Käynnistetään uusi Paper Trader Daemon taustalle (koodiajastin klo {daily_at})...")
    python_exe = sys.executable
    script_path = str(BASE_DIR / "main_controller.py")
    cmd = [python_exe, script_path, "--schedule", "--daily-at", str(daily_at), "--no-initial-run"]

    try:
        if os.name == "nt":
            # DETACHED_PROCESS = 0x00000008, CREATE_NEW_PROCESS_GROUP = 0x00000200
            creationflags = 0x00000008 | 0x00000200
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )

        time.sleep(1.0)
        logs.append(f"✅ Daemon käynnistetty itsenäisenä taustaprosessina (PID: {proc.pid})!")

        # Update daemon status JSON
        try:
            status_payload = {
                "pid": proc.pid,
                "status": "running",
                "mode": "schedule",
                "daily_at": daily_at,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            }
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(DAEMON_STATUS_JSON, status_payload)
        except Exception:
            pass

        return True, "\n".join(logs)

    except Exception as e:
        return False, f"Daemonin käynnistys epäonnistui: {e}"
