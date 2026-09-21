import os
import json
import logging
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from typing import Dict, Any, List, Optional
from openai import OpenAI

logger = logging.getLogger(__name__)

# Severe structural fatal red flags across US, Finnish, and Swedish markets (Instant Hard Exit)
FATAL_RED_FLAG_PATTERNS = [
    # English / US - Severe Insolvency / Fraud
    "going concern",
    "substantial doubt",
    "bankruptcy",
    "chapter 11",
    "liquidation",
    "toxic convertible",
    "sec investigation",
    "sec subpoena",
    "securities fraud",
    "class action lawsuit",
    "restatement of financial",
    # Swedish (SE) - Legal Insolvency / Emergency
    "kontrollbalansräkning",
    "företagsrekonstruktion",
    "rekonstruktion",
    "konkurs",
    "likvidation",
    "ekobrottsmyndigheten",
    # Finnish (FI) - Legal Insolvency / Emergency
    "yrityssaneeraus",
    "saneerausmenettely",
    "saneeraus",
    "konkurssi",
    "selvitystila",
    "kovenanttirikko",
    "finanssivalvonta tutkii",
]

# Secondary warning patterns (dilution, reverse split, compliance) triggering Dual Confirmation
WARN_PATTERNS = [
    # English / US - Dilution / Compliance / Restructuring
    "reverse stock split",
    "reverse split",
    "share consolidation",
    "dilution",
    "warrant overhang",
    "at-the-market offering",
    "atm offering",
    "delisting warning",
    "nasdaq non-compliance",
    "nyse non-compliance",
    # Swedish (SE)
    "företrädesemission",
    "riktad nyemission",
    "riktad emission",
    "nyemission",
    "avnotering",
    "sammanläggning av aktier",
    "omvänd split",
    "utspädning",
    # Finnish (FI)
    "osakeanti",
    "osakeann",
    "suunnattu anti",
    "suunnatun ann",
    "käänteinen split",
    "käänteist",
    "osakkeiden yhdistäminen",
    "osakkeiden yhdistämis",
    "antiasia",
    "laimennus",
    "listalta poistaminen",
]

# Backwards compatibility alias: defaults to FATAL_RED_FLAG_PATTERNS
RED_FLAG_PATTERNS = FATAL_RED_FLAG_PATTERNS

# Key phrases indicating strong positive turnaround catalysts across US, Finnish, and Swedish markets
TURNAROUND_CATALYST_PATTERNS = [
    # English / US
    "major contract win",
    "massive contract",
    "multi-million contract",
    "strategic partnership",
    "major customer",
    "debt restructuring agreement",
    "debt restructured",
    "debt reduction",
    "refinancing secured",
    "credit facility secured",
    "new ceo appointed",
    "new management team",
    "leadership change",
    "appointed as ceo",
    "spinoff of loss-making",
    "divestment of cash-burning",
    "sale of non-core",
    "asset sale to pay debt",
    "strategic turnaround",
    "return to profitability",
    "record revenue growth",
    "positive ebitda guidance",
    # Swedish (SE)
    "stororder",
    "ramavtal",
    "förvärv",
    "företagsförvärv",
    "turnaround",
    "vd-byte",
    "ny vd",
    "positivt rörelseresultat",
    "lönsamhet",
    "omvänd vinstvarning",
    "positiv vinstvarning",
    "skuldsanering",
    "tillväxt",
    # Finnish (FI)
    "suurtilaus",
    "suurtilau",
    "merkittävä sopimus",
    "merkittävän sopimuksen",
    "yrityskauppa",
    "yritysosto",
    "kannattavuuskäänne",
    "uusi toimitusjohtaja",
    "positiivinen tulosvaroitus",
    "tilauskannan kasvu",
    "velkojen uudelleenjärjestely",
    "liiketoimintakauppa",
]

def detect_market(ticker: str, market_override: Optional[str] = None) -> str:
    """
    Detects the target market/exchange region for the ticker.
    Returns 'FI' (Finland), 'SE' (Sweden), or 'US' (United States / Global).
    """
    if market_override:
        norm = market_override.upper().strip()
        if norm in ("FI", "FINLAND", "HE", "HEL"):
            return "FI"
        if norm in ("SE", "SWEDEN", "ST", "STO"):
            return "SE"
        if norm in ("US", "USA", "GLOBAL"):
            return "US"

    t = ticker.upper().strip()
    if t.endswith(".HE") or ".HE" in t or t.endswith(":HE"):
        return "FI"
    if t.endswith(".ST") or t.endswith(".SS") or ".ST" in t or t.endswith(":ST"):
        return "SE"
    return "US"

def fetch_realtime_news(
    ticker: str, 
    company_name: str = "", 
    market: Optional[str] = None,
    api_key: Optional[str] = None
) -> List[str]:
    """
    Convenience wrapper to fetch real-time news across Nordic & US markets using WebSearchVerifier.
    """
    verifier = WebSearchVerifier(api_key=api_key)
    return verifier.fetch_snippets(ticker=ticker, company_name=company_name, market=market)

class WebSearchVerifier:
    """
    Multi-source, Multi-lingual Web Search Verification module.
    Fetches real-time news via Google News RSS (localized for FI, SE, US) & web queries,
    detecting structural red flags and positive turnaround catalysts.
    """
    def __init__(self, api_key: Optional[str] = None, model: str = "google/gemini-2.5-flash"):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.model = model
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

    def fetch_google_news_rss(
        self, 
        query: str, 
        max_results: int = 8, 
        market: str = "US"
    ) -> List[str]:
        """
        Fetches live news headlines and summaries via Google News RSS search feed,
        with localized parameters based on market (FI, SE, US).
        """
        encoded_query = urllib.parse.quote(query)
        
        # Market-specific localization parameters
        if market == "FI":
            locale_params = "hl=fi-FI&gl=FI&ceid=FI:fi"
        elif market == "SE":
            locale_params = "hl=sv-SE&gl=SE&ceid=SE:sv"
        else:
            locale_params = "hl=en-US&gl=US&ceid=US:en"

        url = f"https://news.google.com/rss/search?q={encoded_query}&{locale_params}"
        snippets = []
        try:
            resp = requests.get(url, headers=self.headers, timeout=8)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                for item in root.findall(".//item")[:max_results]:
                    title = item.find("title")
                    desc = item.find("description")
                    title_text = title.text if title is not None and title.text else ""
                    desc_text = ""
                    if desc is not None and desc.text:
                        # Clean html tags from description
                        soup = BeautifulSoup(desc.text, "html.parser")
                        desc_text = soup.get_text(strip=True)
                    
                    combined = f"{title_text}. {desc_text}".strip()
                    if combined:
                        snippets.append(combined)
        except Exception as e:
            logger.debug(f"Google News RSS fetch failed for '{query}' [{market}]: {e}")
        return snippets

    def search_duckduckgo(self, query: str, max_results: int = 5) -> List[str]:
        """
        Fallback scrape of DuckDuckGo HTML search.
        """
        url = "https://html.duckduckgo.com/html/"
        params = {"q": query}
        snippets = []
        try:
            resp = requests.post(url, data=params, headers=self.headers, timeout=8)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                results = soup.find_all("a", class_="result__snippet")
                for r in results[:max_results]:
                    txt = r.get_text(strip=True)
                    if txt:
                        snippets.append(txt)
        except Exception as e:
            logger.debug(f"DuckDuckGo search failed for '{query}': {e}")
        return snippets

    def fetch_snippets(
        self, 
        ticker: str, 
        company_name: str = "", 
        market: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[str]:
        """
        Market-aware multi-source search router:
        - Detects market (FI, SE, US).
        - Queries local financial channels (Cision FI/SE, MFN, GlobeNewswire) & localized Google News RSS.
        - Falls back to native language searches and alternative search engines if initial hits are sparse.
        """
        detected_mkt = detect_market(ticker, market)
        clean_ticker = ticker.split(".")[0] if "." in ticker else ticker
        search_name = company_name.strip() or clean_ticker

        snippets: List[str] = []

        if detected_mkt == "FI":
            # 1. Local Finnish Google News query
            q_fi = f"{search_name} osake uutiset pörssi".strip()
            snippets = self.fetch_google_news_rss(q_fi, max_results=6, market="FI")
            
            # 2. Finnish PR channels / regulatory / risk queries (Cision, Kauppalehti, GlobeNewswire)
            if len(snippets) < 3:
                q_fi_pr = f"{search_name} osakeanti tulosvaroitus tilinpäätös tiedote".strip()
                more = self.fetch_google_news_rss(q_fi_pr, max_results=4, market="FI")
                for m in more:
                    if m not in snippets:
                        snippets.append(m)

            # 3. Fallback to English global query if very few local snippets
            if len(snippets) < 2:
                q_en = f"{clean_ticker} {company_name} stock news".strip()
                en_snippets = self.fetch_google_news_rss(q_en, max_results=4, market="US")
                for m in en_snippets:
                    if m not in snippets:
                        snippets.append(m)

        elif detected_mkt == "SE":
            # 1. Local Swedish Google News query
            q_se = f"{search_name} aktie nyheter börs".strip()
            snippets = self.fetch_google_news_rss(q_se, max_results=6, market="SE")
            
            # 2. Swedish PR channels / regulatory / risk queries (Cision SE, MFN, Placera)
            if len(snippets) < 3:
                q_se_pr = f"{search_name} nyemission rapport pressmeddelande".strip()
                more = self.fetch_google_news_rss(q_se_pr, max_results=4, market="SE")
                for m in more:
                    if m not in snippets:
                        snippets.append(m)

            # 3. Fallback to English global query if very few local snippets
            if len(snippets) < 2:
                q_en = f"{clean_ticker} {company_name} stock news".strip()
                en_snippets = self.fetch_google_news_rss(q_en, max_results=4, market="US")
                for m in en_snippets:
                    if m not in snippets:
                        snippets.append(m)

        else:
            # US / Global
            query1 = f"{ticker} {company_name} stock news".strip()
            snippets = self.fetch_google_news_rss(query1, max_results=6, market="US")
            
            if len(snippets) < 3:
                query2 = f"{ticker} {company_name} reverse split dilution contract earnings".strip()
                more = self.fetch_google_news_rss(query2, max_results=4, market="US")
                for m in more:
                    if m not in snippets:
                        snippets.append(m)

        # General web fallback (DuckDuckGo) if RSS returns empty
        if not snippets:
            ddg_query = f"{ticker} {search_name} stock news"
            snippets = self.search_duckduckgo(ddg_query, max_results=5)

        if max_results and max_results > 0:
            return snippets[:max_results]
        return snippets

    def rule_based_check(self, snippets: List[str]) -> Dict[str, Any]:
        """
        Deterministic keyword filter on scraped snippets for both fatal red flags, warning signals,
        and positive catalysts supporting English, Swedish, and Finnish financial terminology.
        """
        combined_text = " ".join(snippets).lower()
        fatal_flags_found = []
        for pattern in FATAL_RED_FLAG_PATTERNS:
            if pattern.lower() in combined_text:
                fatal_flags_found.append(pattern)

        warnings_found = []
        for pattern in WARN_PATTERNS:
            if pattern.lower() in combined_text:
                warnings_found.append(pattern)

        positive_catalysts_found = []
        for pattern in TURNAROUND_CATALYST_PATTERNS:
            if pattern.lower() in combined_text:
                positive_catalysts_found.append(pattern)

        passed_web_check = len(fatal_flags_found) == 0
        has_warning = len(warnings_found) > 0
        turnaround_catalyst_detected = len(positive_catalysts_found) > 0 and len(fatal_flags_found) == 0

        if not passed_web_check:
            reason = f"Detected fatal red flag keyword(s) in web news: {', '.join(fatal_flags_found)}"
        elif has_warning:
            reason = f"Detected warning signal(s) in web news (requires price confirmation): {', '.join(warnings_found)}"
        elif turnaround_catalyst_detected:
            reason = f"Detected positive turnaround catalyst(s): {', '.join(positive_catalysts_found)}"
        elif snippets:
            reason = f"Live web check clean ({len(snippets)} recent news items reviewed, no red flags)."
        else:
            reason = "No search snippets retrieved (clean or search unavailable)."

        return {
            "passed_web_check": passed_web_check,
            "has_warning": has_warning,
            "fatal_flags_found": fatal_flags_found,
            "warnings_found": warnings_found,
            "red_flags_found": fatal_flags_found,  # backwards compatibility
            "turnaround_catalyst_detected": turnaround_catalyst_detected,
            "positive_catalysts_found": positive_catalysts_found,
            "reason": reason,
            "snippets": snippets,
        }

    def llm_judge_check(
        self, 
        ticker: str, 
        company_name: str, 
        snippets: List[str],
        market: str = "US"
    ) -> Dict[str, Any]:
        """
        Uses LLM to evaluate snippets for critical risks and positive turnaround catalysts
        with multi-lingual comprehension (English, Swedish, Finnish).
        Tries free model first or cascades cleanly to paid model.
        """
        if not self.client or not snippets:
            return self.rule_based_check(snippets)

        prompt = f"""You are a quantitative risk and turnaround equity analyst covering US and Nordic (Finland/Sweden) equity markets.
Analyze the following recent web news headlines and snippets for ticker: {ticker} ({company_name}) [Market: {market}].
Note: The snippets may be in English, Swedish, or Finnish. Accurately interpret local terminology (e.g., Finnish 'osakeanti' as dilution, 'saneeraus' as insolvency restructuring, Swedish 'företrädesemission' as rights issue dilution, 'kontrollbalansräkning' as severe capital shortfall, 'suurtilaus'/'stororder' as massive contract catalyst).

Web News Items:
{chr(10).join(f"- {s}" for s in snippets[:8])}

Check for:
1. FATAL RED FLAGS (Instant Reject): Bankruptcy / liquidation (konkurs / selvitystila / saneeraus), Swedish legal landmines (kontrollbalansräkning / rekonstruktion), securities fraud / SEC subpoenas, toxic death-spiral convertibles.
2. WARNING SIGNALS (Dual Confirmation): Reverse stock splits (käänteinen split / sammanläggning) for exchange compliance, standard rights issues / ATM dilution (osakeanti / företrädesemission / nyemission), exchange non-compliance notices.
3. POSITIVE TURNAROUND CATALYSTS: Massive new contract/deal (suurtilaus / stororder / ramavtal), debt reduction agreement, new tier-1 management/CEO (uusi toimitusjohtaja / vd-byte), profitable corporate acquisition / sale of loss-making unit (yrityskauppa / förvärv), positive profit warning / return to profitability (positiivinen tulosvaroitus / omvänd vinstvarning).

Respond ONLY with valid JSON in the following format:
{{
  "passed_web_check": true/false,
  "has_warning": true/false,
  "fatal_flags_found": ["list of fatal flags, or empty"],
  "warnings_found": ["list of warnings, or empty"],
  "red_flags_found": ["list of fatal flags, or empty"],
  "turnaround_catalyst_detected": true/false,
  "positive_catalysts_found": ["list of positive catalysts, or empty"],
  "reason": "Brief 1-2 sentence explanation of real-time web findings"
}}
"""
        models_to_try = [self.model]
        if ":free" in self.model:
            models_to_try.append("google/gemini-2.5-flash")

        for m_name in models_to_try:
            try:
                resp = self.client.chat.completions.create(
                    model=m_name,
                    messages=[
                        {"role": "system", "content": "You are a strict financial risk & turnaround analyst proficient in English, Swedish, and Finnish financial markets. Output only valid JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0,
                    max_tokens=350,
                )
                # Record Token Usage
                usage = getattr(resp, "usage", None)
                if usage:
                    p_tok = getattr(usage, "prompt_tokens", 0)
                    c_tok = getattr(usage, "completion_tokens", 0)
                    if p_tok > 0 or c_tok > 0:
                        try:
                            from core.token_tracker import record_tokens
                            record_tokens(p_tok, c_tok, model=m_name, source="web_verifier")
                        except Exception:
                            pass

                content = resp.choices[0].message.content.strip()
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()
                
                # Extract clean JSON block if extra text is present
                s_idx = content.find("{")
                e_idx = content.rfind("}")
                if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
                    content = content[s_idx : e_idx + 1]

                res = json.loads(content)
                res["snippets"] = snippets
                return res
            except Exception as e:
                err_msg = str(e)
                if "402" in err_msg or "credits" in err_msg:
                    logger.info(f"OpenRouter credits depleted ({err_msg[:60]}...). Falling back to deterministic rule-based check.")
                    return self.rule_based_check(snippets)
                elif ("429" in err_msg or "502" in err_msg or "503" in err_msg or "504" in err_msg) and len(models_to_try) > 1 and m_name == models_to_try[0]:
                    logger.info(f"⚡ Web verifier free model '{m_name}' busy. Cascading to paid model...")
                    continue
                elif isinstance(e, json.JSONDecodeError) and len(models_to_try) > 1 and m_name == models_to_try[0]:
                    logger.info(f"⚡ Web verifier response from '{m_name}' truncated or invalid JSON. Cascading to paid model...")
                    continue
                else:
                    logger.warning(f"LLM web sanity judge ({m_name}) failed: {e}. Falling back to rule-based check.")
                    return self.rule_based_check(snippets)

        return self.rule_based_check(snippets)

    def verify(
        self, 
        ticker: str, 
        company_name: str = "", 
        market: Optional[str] = None,
        max_results: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Main entry point for Web Search Verification.
        Queries live news across Nordic & US markets and evaluates red flags & turnaround catalysts.
        """
        detected_mkt = detect_market(ticker, market)
        snippets = self.fetch_snippets(ticker, company_name, market=detected_mkt, max_results=max_results)

        if not snippets:
            return {
                "passed": True,
                "passed_web_check": True,
                "red_flags_found": [],
                "turnaround_catalyst_detected": False,
                "positive_catalysts_found": [],
                "reason": "No search snippets retrieved (clean or search unavailable).",
                "snippets": []
            }

        rule_res = self.rule_based_check(snippets)
        rule_res["passed"] = rule_res.get("passed_web_check", True)
        if not rule_res["passed_web_check"]:
            return rule_res

        if self.client:
            llm_res = self.llm_judge_check(ticker, company_name, snippets, market=detected_mkt)
            llm_res["passed"] = llm_res.get("passed_web_check", True)
            return llm_res
        
        return rule_res

