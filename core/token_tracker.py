"""
Token Usage Tracker & Cost Estimation Module.

Persistently tracks input (prompt) and output (completion) tokens across all LLM calls
(Batch Processor, NLP Analyzer, Web Verifier, Live Scanner) and calculates exact costs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TOKEN_FILE = BASE_DIR / "data" / "token_usage.json"

_lock = threading.Lock()

# Model Pricing per 1,000,000 tokens (USD)
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "google/gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "google/gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "google/gemini-2.0-flash-exp:free": {"input": 0.0, "output": 0.0},
    "meta-llama/llama-3.3-70b-instruct": {"input": 0.12, "output": 0.30},
    "meta-llama/llama-3.3-70b-instruct:free": {"input": 0.0, "output": 0.0},
    "meta-llama/llama-3-8b-instruct:free": {"input": 0.0, "output": 0.0},
    "qwen/qwen-2.5-72b-instruct:free": {"input": 0.0, "output": 0.0},
    "anthropic/claude-3.5-haiku": {"input": 0.80, "output": 4.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "default": {"input": 0.10, "output": 0.40},
}

def calculate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    """Calculates estimated cost in USD based on model pricing per 1M tokens."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    input_cost = (prompt_tokens / 1_000_000.0) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000.0) * pricing["output"]
    return input_cost + output_cost

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
    ) -> Dict[str, Any]:
        """
        Records an LLM request's token usage, updates aggregates and saves to file.
        Returns the updated stats dictionary.
        """
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return self.load_stats()

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
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump(stats, f, indent=2)
            except Exception as e:
                logger.error(f"Failed to save token usage: {e}")

            return stats

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
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(empty_stats, f, indent=2)

# Global default instance
tracker = TokenTracker()

def record_tokens(prompt_tokens: int, completion_tokens: int, model: str = "google/gemini-2.5-flash", source: str = "general") -> Dict[str, Any]:
    """Convenience global function to record tokens."""
    return tracker.record_usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, model=model, source=source)

def get_token_stats() -> Dict[str, Any]:
    """Convenience global function to read current token usage stats."""
    return tracker.load_stats()
