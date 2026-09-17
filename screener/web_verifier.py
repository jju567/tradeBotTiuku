import os
import json
import logging
import requests
from bs4 import BeautifulSoup
from typing import Dict, Any, List, Optional
from openai import OpenAI

logger = logging.getLogger(__name__)

# Key phrases indicating severe structural red flags
RED_FLAG_PATTERNS = [
    "reverse stock split",
    "reverse split",
    "share consolidation",
    "going concern",
    "substantial doubt",
    "dilution",
    "toxic convertible",
    "warrant overhang",
    "at-the-market offering",
    "atm offering",
    "sec investigation",
    "sec subpoena",
    "securities fraud",
    "class action lawsuit",
    "restatement of financial",
    "delisting warning",
    "nasdaq non-compliance",
    "nyse non-compliance",
    "bankruptcy",
    "chapter 11",
    "liquidation",
]

class WebSearchVerifier:
    """
    Web Search Verification module.
    Queries search engines for recent news and red flags on a ticker before alert/entry.
    """
    def __init__(self, api_key: Optional[str] = None, model: str = "google/gemini-2.5-flash"):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.model = model
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }
        self.client = None
        if self.api_key:
            try:
                self.client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=self.api_key
                )
            except Exception as e:
                logger.warning(f"Failed to initialize OpenAI client in WebSearchVerifier: {e}")

    def search_duckduckgo(self, query: str, max_results: int = 5) -> List[str]:
        """
        Performs a lightweight scrape of DuckDuckGo HTML search.
        """
        url = "https://html.duckduckgo.com/html/"
        params = {"q": query}
        snippets = []
        try:
            resp = requests.post(url, data=params, headers=self.headers, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                results = soup.find_all("a", class_="result__snippet")
                for r in results[:max_results]:
                    txt = r.get_text(strip=True)
                    if txt:
                        snippets.append(txt)
        except Exception as e:
            logger.warning(f"Web search request failed for '{query}': {e}")
        return snippets

    def rule_based_check(self, snippets: List[str]) -> Dict[str, Any]:
        """
        Deterministic keyword filter on scraped snippets.
        """
        combined_text = " ".join(snippets).lower()
        for pattern in RED_FLAG_PATTERNS:
            if pattern in combined_text:
                return {
                    "passed_web_check": False,
                    "reason": f"Detected red flag keyword in web search: '{pattern}'",
                    "snippets": snippets
                }
        return {
            "passed_web_check": True,
            "reason": "No obvious red flag patterns detected in search snippets.",
            "snippets": snippets
        }

    def llm_judge_check(self, ticker: str, company_name: str, snippets: List[str]) -> Dict[str, Any]:
        """
        Uses LLM to evaluate snippets for critical risks if client is available.
        """
        if not self.client or not snippets:
            return self.rule_based_check(snippets)

        prompt = f"""You are a quantitative risk management auditor.
Analyze the following web search snippets for ticker: {ticker} ({company_name}).

Web Search Snippets:
{chr(10).join(f"- {s}" for s in snippets)}

Check for any severe red flags:
1. Reverse stock splits or chronic share dilution / death spirals.
2. Going concern warnings or near-term bankruptcy risk.
3. Active SEC fraud investigations, major accounting restatements, or delisting notices.
4. Frequent erratic business pivots without organic revenue.

Respond ONLY with valid JSON in the following format:
{{
  "passed_web_check": true/false,
  "red_flags_found": ["string list of any red flags, or empty"],
  "reason": "Brief 1-2 sentence explanation"
}}
"""
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a strict financial risk analyst. Output only valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0,
                max_tokens=500,
            )
            content = resp.choices[0].message.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            res = json.loads(content)
            res["snippets"] = snippets
            return res
        except Exception as e:
            logger.warning(f"LLM web sanity judge failed or timed out: {e}. Falling back to rule-based check.")
            return self.rule_based_check(snippets)

    def verify(self, ticker: str, company_name: str = "") -> Dict[str, Any]:
        """
        Main entry point for Web Search Verification.
        Queries web search and performs rule-based and LLM verification.
        """
        query = f'"{ticker}" {company_name} stock reverse split dilution warning news'.strip()
        snippets = self.search_duckduckgo(query, max_results=5)
        
        if not snippets:
            query2 = f"{ticker} stock financial trouble SEC going concern".strip()
            snippets = self.search_duckduckgo(query2, max_results=5)

        if not snippets:
            return {
                "passed_web_check": True,
                "reason": "No search snippets retrieved (clean or search unavailable).",
                "snippets": []
            }

        rule_res = self.rule_based_check(snippets)
        if not rule_res["passed_web_check"]:
            return rule_res

        if self.client:
            return self.llm_judge_check(ticker, company_name, snippets)
        
        return rule_res
