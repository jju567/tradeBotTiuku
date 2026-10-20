"""
tests/test_server_maintenance.py - Unit tests for Server Remote Maintenance & Process Lifecycle Manager.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.server_maintenance import (
    find_daemon_pids,
    get_daemon_status,
    get_git_info,
    git_pull,
    is_pid_alive,
    restart_daemon,
)


def test_get_git_info_mocked():
    with patch("core.server_maintenance._run_cmd") as mock_cmd:
        # Mock git commands in order: is-inside, branch, hash, log, remote, status
        mock_cmd.side_effect = [
            (0, "true", ""),                                 # rev-parse --is-inside-work-tree
            (0, "main", ""),                                 # rev-parse --abbrev-ref HEAD
            (0, "a1b2c3d", ""),                             # rev-parse --short HEAD
            (0, "feat: update||2026-10-20 10:00:00||5m ago", ""), # git log -1
            (0, "https://github.com/org/repo.git", ""),      # remote.origin.url
            (0, " M file.py\n?? untracked.txt", ""),         # status --porcelain
        ]

        info = get_git_info()
        assert info["is_git"] is True
        assert info["branch"] == "main"
        assert info["commit_hash"] == "a1b2c3d"
        assert info["commit_msg"] == "feat: update"
        assert info["is_dirty"] is True
        assert info["dirty_files_count"] == 1


def test_get_git_info_not_a_repo():
    with patch("core.server_maintenance._run_cmd") as mock_cmd:
        mock_cmd.return_value = (1, "", "fatal: not a git repository")
        info = get_git_info()
        assert info["is_git"] is False
        assert info["branch"] == "unknown"


def test_git_pull_success():
    with patch("core.server_maintenance.get_git_info") as mock_info, \
         patch("core.server_maintenance._run_cmd") as mock_cmd:
        mock_info.side_effect = [
            {"is_git": True, "branch": "main"},
            {"is_git": True, "branch": "main", "commit_hash": "c0ffee1", "commit_msg": "latest commit"},
        ]
        # fetch, pull
        mock_cmd.side_effect = [
            (0, "Fetched origin/main", ""),
            (0, "Updating a1b2c3d..c0ffee1\nFast-forward", ""),
        ]

        success, logs = git_pull()
        assert success is True
        assert "Fast-forward" in logs
        assert "c0ffee1" in logs


def test_git_pull_failure():
    with patch("core.server_maintenance.get_git_info") as mock_info, \
         patch("core.server_maintenance._run_cmd") as mock_cmd:
        mock_info.return_value = {"is_git": True, "branch": "main"}
        mock_cmd.return_value = (1, "", "fatal: unable to access remote")

        success, logs = git_pull()
        assert success is False
        assert "Git fetch epäonnistui" in logs


def test_get_daemon_status_mocked(tmp_path):
    status_file = tmp_path / "daemon_status.json"
    status_file.write_text(json.dumps({
        "pid": 99999,
        "status": "running",
        "daily_at": "23:05",
        "started_at": "2026-10-20T10:00:00Z",
        "last_heartbeat": "2026-10-20T10:05:00Z",
    }), encoding="utf-8")

    with patch("core.server_maintenance.DAEMON_STATUS_JSON", status_file), \
         patch("core.server_maintenance.find_daemon_pids", return_value=[99999]):
        st = get_daemon_status()
        assert st["is_running"] is True
        assert 99999 in st["pids"]
        assert "🟢" in st["status_text"]


def test_restart_daemon_systemd():
    with patch("core.server_maintenance.os.name", "posix"), \
         patch("core.server_maintenance.find_daemon_pids", return_value=[]), \
         patch("core.server_maintenance._run_cmd") as mock_cmd, \
         patch("core.server_maintenance.get_daemon_status") as mock_status:
        # systemctl status, sudo systemctl restart
        mock_cmd.side_effect = [
            (0, "Loaded: loaded (/etc/systemd/...)", ""),
            (0, "Restarting...", ""),
        ]
        mock_status.return_value = {"status_text": "🟢 Käynnissä systemd-palveluna (tradebot-tiuku-daemon)"}

        ok, msg = restart_daemon(daily_at="23:05")
        assert ok is True
        assert "Systemd-palvelu 'tradebot-tiuku-daemon' käynnistetty uudelleen" in msg


def test_restart_daemon_standalone():
    with patch("core.server_maintenance.os.name", "nt"), \
         patch("core.server_maintenance.find_daemon_pids", return_value=[]), \
         patch("core.server_maintenance.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_popen.return_value = mock_proc

        ok, msg = restart_daemon(daily_at="23:05")
        assert ok is True
        assert "itsenäisenä taustaprosessina" in msg
        assert "54321" in msg
