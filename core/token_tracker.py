"""
Token Usage Tracker & Cost Estimation Module.

Persistently tracks input (prompt) and output (completion) tokens across all LLM calls
(Batch Processor, NLP Analyzer, Web Verifier, Live Scanner) and calculates exact costs.
Provides real-time OpenRouter account balance and usage queries.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TOKEN_FILE = BASE_DIR / "data" / "token_usage.json"

_lock = threading.Lock()
_openrouter_lock = threading.Lock()
_openrouter_cache: Dict[str, Any] = {"timestamp": 0.0, "data": None}


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


# Model Pricing per 1,000,000 tokens (USD)
# Verified from OpenRouter API (/api/v1/models)
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "google/gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "google/gemini-2.5-flash-image": {"input": 0.30, "output": 2.50},
    "google/gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "google/gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "google/gemini-2.0-flash-exp:free": {"input": 0.0, "output": 0.0},
    "meta-llama/llama-3.3-70b-instruct": {"input": 0.12, "output": 0.30},
    "meta-llama/llama-3.3-70b-instruct:free": {"input": 0.0, "output": 0.0},
    "meta-llama/llama-3-8b-instruct:free": {"input": 0.0, "output": 0.0},
    "qwen/qwen-2.5-72b-instruct:free": {"input": 0.0, "output": 0.0},
    "anthropic/claude-3.5-haiku": {"input": 0.80, "output": 4.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "default": {"input": 0.30, "output": 2.50},
}


def calculate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    """Calculates estimated cost in USD based on model pricing per 1M tokens."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    input_cost = (prompt_tokens / 1_000_000.0) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000.0) * pricing["output"]
    return input_cost + output_cost


def fetch_openrouter_balance(api_key: Optional[str] = None, max_age_seconds: int = 60) -> Optional[Dict[str, Any]]:
    """
    Fetches real-time credit balance and usage stats from OpenRouter API.
    Caches the response for max_age_seconds (default 60s) to avoid excessive API requests.
    """
    global _openrouter_cache
    now = time.time()
    with _openrouter_lock:
        if _openrouter_cache["data"] is not None and (now - _openrouter_cache["timestamp"]) < max_age_seconds:
            return _openrouter_cache["data"]

    key = api_key or os.getenv("OPENROUTER_API_KEY") or ""
    if not key:
        return None

    try:
        import requests
        headers = {"Authorization": f"Bearer {key.strip()}"}

        # 1. Credits endpoint (total credits purchased & total usage)
        res_credits = requests.get("https://openrouter.ai/api/v1/credits", headers=headers, timeout=5.0)
        credits_data = res_credits.json().get("data", {}) if res_credits.status_code == 200 else {}
        total_credits = float(credits_data.get("total_credits", 0.0))
        total_usage_cred = float(credits_data.get("total_usage", 0.0))

        # 2. Key usage endpoint (daily, weekly, monthly breakdown)
        res_key = requests.get("https://openrouter.ai/api/v1/auth/key", headers=headers, timeout=5.0)
        key_data = res_key.json().get("data", {}) if res_key.status_code == 200 else {}
        total_usage_key = float(key_data.get("usage", total_usage_cred))
        usage_daily = float(key_data.get("usage_daily", 0.0))
        usage_weekly = float(key_data.get("usage_weekly", 0.0))
        usage_monthly = float(key_data.get("usage_monthly", 0.0))
        label = str(key_data.get("label", ""))

        balance = max(0.0, total_credits - total_usage_cred) if total_credits > 0 else 0.0

        result = {
            "total_credits": round(total_credits, 4),
            "total_usage": round(total_usage_cred or total_usage_key, 4),
            "balance": round(balance, 4),
            "usage_daily": round(usage_daily, 4),
            "usage_weekly": round(usage_weekly, 4),
            "usage_monthly": round(usage_monthly, 4),
            "key_label": label,
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

        with _openrouter_lock:
            _openrouter_cache = {"timestamp": now, "data": result}
        return result
    except Exception as e:
        logger.debug(f"Failed to fetch OpenRouter balance: {e}")
        return None


def recalculate_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recalculates cost_usd across all models and sources in stats dictionary
    using the updated MODEL_PRICING.
    """
    total_cost = 0.0
    by_model = stats.get("by_model", {})
    for m_name, m_data in by_model.items():
        p_tok = m_data.get("prompt_tokens", 0)
        c_tok = m_data.get("completion_tokens", 0)
        c = calculate_cost(p_tok, c_tok, m_name)
        m_data["cost_usd"] = c
        total_cost += c

    by_source = stats.get("by_source", {})
    for s_name, s_data in by_source.items():
        p_tok = s_data.get("prompt_tokens", 0)
        c_tok = s_data.get("completion_tokens", 0)
        s_data["cost_usd"] = calculate_cost(p_tok, c_tok, "google/gemini-2.5-flash")

    stats["total_cost_usd"] = total_cost
    stats["last_updated"] = datetime.now(timezone.utc).isoformat()
    return stats


class TokenTracker:
    """Persistent tracker for token usage and costs."""

    def __init__(self, file_path: Path = DEFAULT_TOKEN_FILE):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def load_stats(self) -> Dict[str, Any]:
        """Loads token usage stats from JSON file."""
        if not self.file_path.exists() or self.file_path.stat().st_size == 0:
            return {
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "total_requests": 0,
                "total_cost_usd": 0.0,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "by_model": {},
                "by_source": {},
            }
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to read token stats: {e}")
            return {
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "total_requests": 0,
                "total_cost_usd": 0.0,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "by_model": {},
                "by_source": {},
            }

    def record_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str = "google/gemini-2.5-flash",
        source: str = "general",
        cost_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Records an LLM request's token usage, updates aggregates and saves to file.
        Returns the updated stats dictionary.
        """
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return self.load_stats()

        if cost_override is not None and cost_override > 0:
            cost = float(cost_override)
        else:
            cost = calculate_cost(prompt_tokens, completion_tokens, model)
        total_tokens = prompt_tokens + completion_tokens

        with _lock:
            stats = self.load_stats()
            stats["total_prompt_tokens"] = stats.get("total_prompt_tokens", 0) + prompt_tokens
            stats["total_completion_tokens"] = stats.get("total_completion_tokens", 0) + completion_tokens
            stats["total_tokens"] = stats.get("total_tokens", 0) + total_tokens
            stats["total_requests"] = stats.get("total_requests", 0) + 1
            stats["total_cost_usd"] = stats.get("total_cost_usd", 0.0) + cost
            stats["last_updated"] = datetime.now(timezone.utc).isoformat()

            # By Model
            by_model = stats.setdefault("by_model", {})
            m_stat = by_model.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0, "cost_usd": 0.0})
            m_stat["prompt_tokens"] += prompt_tokens
            m_stat["completion_tokens"] += completion_tokens
            m_stat["total_tokens"] += total_tokens
            m_stat["requests"] += 1
            m_stat["cost_usd"] += cost

            # By Source
            by_source = stats.setdefault("by_source", {})
            s_stat = by_source.setdefault(source, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0, "cost_usd": 0.0})
            s_stat["prompt_tokens"] += prompt_tokens
            s_stat["completion_tokens"] += completion_tokens
            s_stat["total_tokens"] += total_tokens
            s_stat["requests"] += 1
            s_stat["cost_usd"] += cost

            try:
                _atomic_write_json(self.file_path, stats)
            except Exception as e:
                logger.error(f"Failed to save token usage: {e}")

            return stats

    def recalculate_file(self) -> Dict[str, Any]:
        """Recalculates all costs in the current JSON file using latest pricing and saves."""
        with _lock:
            stats = self.load_stats()
            updated_stats = recalculate_stats(stats)
            try:
                _atomic_write_json(self.file_path, updated_stats)
            except Exception as e:
                logger.error(f"Failed to save recalculated token usage: {e}")
            return updated_stats

    def reset_stats(self) -> None:
        """Resets all token usage stats to zero."""
        empty_stats = {
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "total_requests": 0,
            "total_cost_usd": 0.0,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "by_model": {},
            "by_source": {},
        }
        with _lock:
            try:
                _atomic_write_json(self.file_path, empty_stats)
            except Exception as e:
                logger.error(f"Failed to reset token usage: {e}")


# Global default instance
tracker = TokenTracker()


def record_tokens(
    prompt_tokens: int,
    completion_tokens: int,
    model: str = "google/gemini-2.5-flash",
    source: str = "general",
    cost_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Convenience global function to record tokens."""
    return tracker.record_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
        source=source,
        cost_override=cost_override,
    )


def get_token_stats() -> Dict[str, Any]:
    """Convenience global function to read current token usage stats."""
    return tracker.load_stats()


def recalculate_stored_tokens() -> Dict[str, Any]:
    """Convenience global function to recalculate costs in stored file."""
    return tracker.recalculate_file()
