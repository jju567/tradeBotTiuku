"""
NLP & LLM Analysis Module for Financial News & Regulatory Filings using OpenRouter API.

Supports two distinct screening pipelines:
1. SATELLITE (Daily Catalyst Hunting):
   - Ingests daily RSS releases, PRs, MAR Manager's transactions.
   - Extracts: `insider_buying_personal`, `positive_guidance`, `company_share_buyback`, `cash_issue`.
2. CORE (Quarterly Fundamental Tenbagger Hunting):
   - Ingests Earnings Reports (Osavuosikatsaukset) & Annual Reports (Tilinpäätöstiedotteet / Tilinpäätökset) and attached PDFs.
   - Evaluates the 6-point Growth & Fundamental Quality Checklist:
     (1) Accelerating growth rate, (2) Room to grow / TAM, (3) Management skin-in-the-game,
     (4) Margin trajectory / operating leverage, (5) Revenue quality (Recurring/SaaS vs project-based),
     (6) Cash runway without massive equity dilution (Risk screen first).
   - Core Filters: Gross Margin > 40%, Recurring Revenue, Rule of 40 (Growth % + Margin % >= 40%).

Includes OpenRouter API integration, rate limiting with exponential backoff, chunking, and deterministic fallbacks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional, Dict, Any, List

import requests

from .config import ScreenerConfig
from .models import ScrapedRelease, NLPExtractionResult, StrategyType
from .financial_metrics_engine import get_hard_financials, format_for_llm_prompt

logger = logging.getLogger(__name__)

# OpenRouter API Endpoints and Defaults
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3-8b-instruct:free"

# Rate Limiting Configuration
DEFAULT_THROTTLE_SLEEP = 7.0      # Mandatory politeness delay between successful API requests (seconds)
MAX_RETRIES = 3                   # Maximum retries on 429 or 5xx errors
RETRY_BACKOFF_DELAYS = [5.0, 10.0, 20.0]  # Progressive sleep on rate-limit breaches (seconds)

SAFE_DEFAULT_RESPONSE: Dict[str, Any] = {
    "insider_buying_personal": False,
    "company_share_buyback": False,
    "cash_issue": False,
    "positive_guidance": False,
    "reasoning": "Ei havaittuja merkittäviä signaaleja tai poikkeamia pörssitiedotteessa.",
    "management_buying": False,
}

SAFE_DEFAULT_CORE_RESPONSE: Dict[str, Any] = {
    "profile_A_growth": {
        "gross_margin_over_40": False,
        "rule_of_40_passed": False,
        "recurring_revenue_mentioned": False,
        "organic_growth_confirmed": False,
    },
    "profile_B_value": {
        "strong_net_cash_position": False,
        "positive_operating_cash_flow": False,
        "turnaround_indicators": False,
        "revenue_shrinking": False,
    },
    "financial_safety": {
        "going_concern_risk": False,
        "dilution_risk_detected": False,
        "unsustainable_cash_burn": False,
        "erratic_pivots_detected": False,
    },
    "verdict_details": {
        "matched_profile": "NONE",
        "verdict": "REJECT",
        "reasoning": "Ei täytä Core-salkun kriteerejä (Profile A Kasvu tai Profile B Arvo/Käänne).",
    },
}

# ----------------------------------------------------------------------
# SYSTEM PROMPTS
# ----------------------------------------------------------------------

SATELLITE_SYSTEM_PROMPT = """You are an expert quantitative financial analyst and pedagogical mentor specializing in Nordic and Helsinki Nasdaq micro-cap disclosures.
Analyze the provided company disclosure text and extract catalyst signals and a clear, educational rationale.

RULES:
1. "company_share_buyback": Set to TRUE ONLY if the text mentions 'omien osakkeiden hankinta' (acquisition of own shares), 'share buyback program', or a broker acquiring shares on behalf of the COMPANY. When TRUE, set "insider_buying_personal" to FALSE.
2. "insider_buying_personal": Set to TRUE ONLY if the text is a 'Johdon liiketoimet' (Manager's transactions / MAR notification) and explicitly states that an individual person (e.g., CEO, Board Member, CFO) has purchased shares ('hankinta' / 'merkintä' / 'purchase' / 'subscription') with their personal funds from the open market. Distinguish from options, incentive schemes, or emergency bridge loans.
3. "cash_issue": Set to TRUE if there are explicit mentions of severe liquidity distress, running out of working capital ('käyttöpääoma loppunut', 'maksuvalmius heikentynyt'), immediate emergency financing needs, going concern warnings, or emergency dilutive bridge financing.
4. "positive_guidance": Set to TRUE if the company is raising its revenue/profit guidance or issuing a positive profit warning ('positiivinen tulosvaroitus', 'nostaa ohjeistustaan', 'upgrades guidance'). Set to FALSE if negative profit warning or lowered outlook.
5. "reasoning": Write a concise, structured Finnish pedagogical explanation that breaks down the decision step-by-step into concrete components (a), (b), (c) and highlights any trade-offs or contradictions.
   Format requirement:
   - If positive signal: "Tämä yhtiö nousi listalle koska (a) [tarkka havainto johdon ostosta / tulosparannuksesta lukuineen/henkilöineen], (b) [taloudellinen tila tai markkinakonteksti], (c) [mahdollinen riski, ristiriita tai huomioitava tekijä]."
   - If rejected / neutral: "Tämä yhtiö hylättiin / jätettiin neutraaliksi koska (a) [syy miksi ei täytä kriteerejä tai riskitekijä]."

OUTPUT FORMAT:
Output strictly a valid JSON object with no markdown outside JSON:
{
  "insider_buying_personal": bool,
  "company_share_buyback": bool,
  "cash_issue": bool,
  "positive_guidance": bool,
  "reasoning": string
}"""

# Backwards compatibility alias
SYSTEM_PROMPT = SATELLITE_SYSTEM_PROMPT

CORE_SYSTEM_PROMPT = """You are an elite quantitative micro/small-cap equity analyst and risk officer specializing in global equities (US NASDAQ/NYSE under $300M market cap, and Nordic markets .HE, .ST, .OL, .CO). Your task is to evaluate financial documents (10-Q, 10-K, interim reports, regulatory filings) and recent news headlines/PR snippets.

Your primary directive is CAPITAL PRESERVATION: rigorously identify and reject toxic financing, ongoing dilution, unsustainable cash burn, and structural value traps before hunting for asymmetric upside.

---

# SECTION 1: FATAL CAPITAL DESTRUCTION FILTERS (INSTANT REJECT)
If ANY of the following conditions are met, the stock MUST receive a verdict of "REJECT", regardless of net cash position or potential narrative:

1. INSOLVENCY & LEGAL EMERGENCIES:
   - Chapter 11, bankruptcy, liquidation, or formal debt restructuring / court receivership (saneeraus / konkurs).
   - SWEDISH LEGAL LANDMINES (.ST): Mentions of "kontrollbalansräkning" or "företagsrekonstruktion" (equity below half of registered capital).
   - Regulatory fraud, SEC/Finansinspektionen subpoena/investigation into accounting fraud, or restatement of financials.
   - Toxic debt / death-spiral convertible notes with variable conversion floors.

2. ERRATIC STRATEGIC PIVOTS / EMPTY SHELLS:
   - Sudden, unrelated business model pivots (e.g., pivot from retail/cosmetics to AI, crypto, mining within a short timeframe).
   - SPAC empty shells without commercial operating assets.

3. NEGATIVE SHAREHOLDERS' EQUITY:
   - Deeply negative book equity resulting from cumulative operational losses, unless explicitly offset by non-recourse project structures.

---

# SECTION 1B: STRUCTURAL WARNINGS & COMPLIANCE ACTIONS (VERDICT: "WARN" or "WATCH_TURNAROUND")
Do NOT instantly REJECT for the following if the company has high gross margins (>40%), growing commercial revenue, or active turnaround catalysts. Instead, assign verdict "WARN":

1. REVERSE STOCK SPLITS:
   - A reverse stock split proposed or executed primarily to regain exchange compliance (e.g., Nasdaq $1.00 minimum bid price rule) is a WARNING ("WARN"), NOT an instant REJECT. Viable micro-caps frequently use reverse splits to preserve listing while growing.
2. CASH RUNWAY & ROUTINE DILUTION:
   - Cash runway < 4 quarters in early-stage commercial growth companies (gross margin > 40% or positive revenue growth) or biotech with active partnership catalysts should be flagged as "WARN", not "REJECT".
   - Standard rights issues (företrädesemission / osakeanti) or ATM continuous facilities used for growth investments rather than emergency bankruptcy evasion receive "WARN".

---

# SECTION 2: DUAL-LENS INVESTMENT PROFILES
Only stocks passing ALL Section 1 filters can be evaluated for Profile A or Profile B:

### PROFILE A: HIGH-MARGIN COMPOUNDER (GROWTH / SAAS)
- Gross Margin > 40% (preferably > 60%).
- Organic revenue growth YoY (> 15% preferred).
- High recurring revenue (SaaS, subscriptions, software maintenance) or expanding high-margin hardware/product footprint.
- Clear path to or already positive operating cash flow.

### PROFILE B: DEEP VALUE / OPERATIONAL TURNAROUND
- Pristine or highly defended balance sheet (Net Cash positive or Debt/Equity <= 0.3).
- Positive operating cash flow OR demonstrable narrowing of operating losses via structural cost reductions.
- STRICT ANTI-SHRINKING RULE: A zero-debt balance sheet alone is INSUFFICIENT. If core business metrics, revenue, or active users/customers are contracting year-over-year without an active operational fix, DO NOT award STRONG BUY.
- Catalyst Requirement: If past quarters were unprofitable, there must be a tangible operational catalyst (tier-1 management replacement, divesting of money-losing segments, or strategic accretive contracts).

---

# SECTION 3: REAL-TIME DIVERGENCE & VERDICT ROUTING
1. "STRONG BUY": Passes all Section 1 safety filters, satisfies Profile A or B, and web news confirms clean operational momentum.
2. "HOLD": Financially solvent and safe, but lacks growth momentum, high margins, or compelling catalysts.
3. "WARN": Structural dilution, reverse split for Nasdaq compliance, or cash runway < 4 quarters in an otherwise active/growing company. Activates Dual Confirmation (requires price confirmation before exit).
4. "REJECT": Fails Section 1 fatal filters (bankruptcy, Chapter 11, kontrollbalansräkning, accounting fraud, toxic death spirals).
5. "WATCH_TURNAROUND": Document analysis yields poor historical figures, BUT recent verified web news reveals a major turnaround catalyst (CEO/insider buying, multi-million contracts, structural restructuring).

---

# SECTION 4: OUTPUT FORMAT SPECIFICATION
Respond exclusively with a valid, parseable JSON object matching this schema:
{
  "ticker": "string",
  "market": "US" | "FI" | "SE" | "OTHER",
  "profile": "PROFILE_A" | "PROFILE_B" | "NONE",
  "verdict": "STRONG BUY" | "HOLD" | "REJECT" | "WATCH_TURNAROUND",
  "flags": {
    "dilution_risk_detected": bool,
    "unsustainable_cash_burn": bool,
    "erratic_pivots_detected": bool,
    "shrinking_business": bool,
    "organic_growth_confirmed": bool,
    "passed_web_sanity_check": bool
  },
  "metrics": {
    "gross_margin_pct": number_or_null,
    "revenue_growth_yoy_pct": number_or_null,
    "cash_runway_quarters": number_or_null,
    "net_cash_positive": bool
  },
  "pedagogical_reasoning": "Concise, fact-based rationale (2-4 sentences) outlining exact balance sheet conditions, cash burn figures, and the impact of the latest news snippets. ALWAYS refer to the company by its FULL formal corporate name (e.g. 'Atomera Incorporated', 'Vivid Seats Inc.', 'Faron Pharmaceuticals Oyj') rather than bare ticker symbols."
}"""

# ---------------------------------------------------------------------------
# MASTER SYSTEM PROMPT (Deterministic First Architecture)
# ---------------------------------------------------------------------------

MASTER_SYSTEM_PROMPT = """
Olet monikielinen (fi-FI, sv-SE, en-US) osakeseulonta-analyytikko.

SECTION 0: HARD FINANCIAL FACTS - EHDOTON SÄÄNTÖ
Saat käyttäjäviestissä osion "[HARD FINANCIAL FACTS - DO NOT RECALCULATE]".
Nämä luvut on laskettu deterministisesti Pythonilla suoraan raakadatasta.
- ÄLÄ laske, arvioi tai korjaa näitä lukuja millään tavalla.
- ÄLÄ käytä dokumentin (10-Q/10-K/pörssitiedote) tekstissä mainittuja lukuja,
  jos ne ovat ristiriidassa annettujen Hard Financial Facts -lukujen kanssa.
  Mainitse ristiriita analyysissasi, mutta luota annettuihin lukuihin.
- Jos jokin kenttä on merkitty "EI SAATAVILLA", älä oleta arvoa (älä koskaan
  tulkitse puuttuvaa dataa nollaksi tai neutraaliksi signaaliksi). Merkitse
  epävarmuus eksplisiittisesti "confidence"-kentässä.
- Käytä dokumentin tekstiä VAIN laadulliseen kontekstiin: johdon kommentaarit,
  riskitekijät, yrityskaupat, saneeraukset, katalyytit.

SECTION 1: MANDATORY HARD FILTERS (Automaattinen REJECT)
Hylkää yhtiö automaattisesti (judgment="REJECT") jos JOKIN seuraavista pätee:
- Aktiivinen osakeantiriski / ATM-ohjelma joka ei ole selvästi peruttu/päättynyt.
- cash_runway_months < 6 JA ei mainintaa vahvistetusta ulkoisesta rahoituksesta.
- Epämääräinen tai selittämätön liiketoimintakäänne ilman konkreettista näyttöä.
- Hallitsematon velkaantuminen (net_cash selvästi ja jatkuvasti negatiivinen
  ilman uskottavaa lyhennyssuunnitelmaa).
- RUOTSIN MARKKINA (.ST) - LISÄSÄÄNTÖ: jos teksti mainitsee sanan
  "kontrollbalansräkning" TAI "rekonstruktion" missä tahansa muodossa
  (myös taivutettuna), tee VÄLITTÖMÄSTI judgment="REJECT" riippumatta
  muista tunnusluvuista. Tämä on juridinen hätätila (oma pääoma alle puolet
  rekisteröidystä osakepääomasta) joka ohittaa kaikki muut signaalit.

SECTION 2: TUOMIOSYNTEESI
Palauta JSON-muodossa: judgment (STRONG_BUY | WATCH_TURNAROUND | HOLD | REJECT),
confidence (0-1), pedagogical_reasoning (täsmälliset perustelut viitaten
suoraan annettuihin Hard Financial Facts -lukuihin ja tekstin laadullisiin
havaintoihin), triggered_hard_filters (lista, tyhjä jos ei mitään).
"""


def build_analysis_prompt(ticker: str, market: str = "US", news_text: str = "", filing_text: str = "") -> str:
    """
    Kokoaa käyttäjäviestin LLM:lle: ensin kovat luvut (deterministiset),
    sitten laadullinen teksti (uutiset + tiedote/raportti).

    market: esim. "FI", "SE", "US" - käytetään lähinnä lokalisointiin ja
    Ruotsin erityissäännön kontekstointiin promptin sisällä.
    """
    hard_facts = get_hard_financials(ticker)
    hard_facts_text = format_for_llm_prompt(hard_facts)

    prompt = f"""
[HARD FINANCIAL FACTS - DO NOT RECALCULATE]
{hard_facts_text}

[MARKET]
{market}

[NEWS / PÖRSSITIEDOTTEET]
{news_text}

[FILING TEXT / TILINPÄÄTÖSDOKUMENTTI - LAADULLISEEN KONTEKSTIIN]
{filing_text}

Analysoi yllä olevan perusteella SECTION 0-2 -ohjeiden mukaisesti.
Muista: Hard Financial Facts -osio on ehdoton totuus, älä laske uudelleen.
"""
    return prompt.strip()


def analyze_company(
    ticker: str,
    market: str = "US",
    news_text: str = "",
    filing_text: str = "",
    llm_client: Any = None,
) -> Dict[str, Any]:
    """
    Analysoi yhtiön käyttäen MASTER_SYSTEM_PROMPT:ia ja build_analysis_prompt:ia.
    Jos llm_client annetaan, käyttää sitä. Muussa tapauksessa kutsuu sisäistä OpenRouter API -wrapperia.
    """
    user_prompt = build_analysis_prompt(ticker=ticker, market=market, news_text=news_text, filing_text=filing_text)

    if llm_client and hasattr(llm_client, "generate"):
        return llm_client.generate(
            system_prompt=MASTER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
        )

    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)

    if api_key:
        parsed = _call_openrouter_api(
            prompt=user_prompt,
            system_prompt=MASTER_SYSTEM_PROMPT,
            api_key=api_key,
            model=model,
        )
        if parsed:
            return parsed

    return rule_based_analyze_core_fundamentals(f"{news_text}\n{filing_text}")


def route_document(
    news_item: Any,
    has_pdf: bool = False,
    full_text: str = "",
) -> str:
    """
    Intelligent Document Router:
    Classifies an incoming scraped release into:
    - 'TRASH': Administrative noise, AGM invitations, board meeting minutes, share schemes, calendar notices.
    - 'CORE': Quarterly earnings reports, annual statements, interim reports, or financial report PDFs.
    - 'SATELLITE': Daily catalyst news, insider transactions, profit warnings, contract awards, PR updates.

    Accepts FeedItem, dictionary, or string title.
    """
    if isinstance(news_item, str):
        title = news_item
        category = ""
        summary = ""
    elif hasattr(news_item, "title"):  # FeedItem or dataclass
        title = getattr(news_item, "title", "") or ""
        category = getattr(news_item, "category", "") or ""
        summary = getattr(news_item, "summary", "") or ""
    elif isinstance(news_item, dict):
        title = news_item.get("title", "") or ""
        category = news_item.get("category", "") or ""
        summary = news_item.get("summary", "") or ""
        has_pdf = has_pdf or news_item.get("has_pdf", False) or news_item.get("pdf_attached", False)
    else:
        title = str(news_item)
        category = ""
        summary = ""

    text_to_check = f"{title} {category} {summary}".lower()

    # 1. TRASH GATEKEEPER (Discard administrative & routine noise to save LLM credits)
    trash_patterns = [
        # AGM / General Meeting Invitations & Notices (FI, SE, EN)
        r"kutsu\s+(?:varsinaiseen|ylimääräiseen)?\s*yhtiökokoukseen",
        r"kutsu\s+yhtiökokoukseen",
        r"yhtiökokouksen\s+päätökset",
        r"varsinaisen\s+yhtiökokouksen\s+päätökset",
        r"ylimääräisen\s+yhtiökokouksen\s+päätökset",
        r"hallituksen\s+järjestäytyminen",
        r"nimitystoimikunnan\s+ehdotuk",
        r"nimitystoimikunnan\s+ehdotus",
        r"nimitysvaliokunnan\s+ehdotuk",
        r"nimitysvaliokunnan\s+ehdotus",
        r"kallelse\s+till\s+(?:extra|ordinarie)?\s*bolagsstämma",
        r"kallelse\s+till\s+årsstämma",
        r"beslut\s+vid\s+(?:extra|ordinarie)?\s*bolagsstämma",
        r"beslut\s+vid\s+årsstämma",
        r"kommuniké\s+från\s+(?:extra|ordinarie)?\s*bolagsstämma",
        r"kommuniké\s+från\s+årsstämma",
        r"konstituerande\s+styrelsemöte",
        r"valberedningens\s+förslag",
        r"notice\s+to\s+(?:the\s+)?(?:annual|extraordinary)?\s*general\s+meeting",
        r"notice\s+of\s+(?:the\s+)?(?:annual|extraordinary)?\s*general\s+meeting",
        r"resolutions\s+of\s+(?:the\s+)?(?:annual|extraordinary)?\s*general\s+meeting",
        r"decisions\s+of\s+(?:the\s+)?(?:annual|extraordinary)?\s*general\s+meeting",
        r"constitutive\s+meeting\s+of\s+the\s+board",
        r"proposals?\s+of\s+the\s+nomination\s+committee",
        # Administrative, incentive schemes, calendars & routine filings
        r"osakepalkkiojärjestelmä",
        r"kannustinjärjestelmä",
        r"incitamentsprogram",
        r"aktiesparprogram",
        r"share-based\s+incentive\s+plan",
        r"taloudellinen\s+kalenteri",
        r"taloudellisen\s+katsauksen\s+julkistamisajankohdat",
        r"julkistamiskalenteri",
        r"finansiell\s+kalender",
        r"financial\s+calendar",
        r"reporting\s+calendar",
        r"liputusilmoitus",
        r"liputusilmoitukset",
        r"flaggningsmeddelande",
        r"flagging\s+notification",
    ]

    for pat in trash_patterns:
        if re.search(pat, text_to_check):
            return "TRASH"

    # 2. CORE PATTERNS (Earnings Reports, Financial Statements, Annual Reports)
    core_patterns = [
        # Finnish
        r"osavuosikatsaus",
        r"puolivuosikatsaus",
        r"tilinpäätöstiedote",
        r"tilinpäätös",
        r"vuosikertomus",
        r"tilinpäätösraportti",
        r"liiketoimintakatsaus",
        r"tammi-maaliskuu",
        r"tammi-kesäkuu",
        r"tammi-syyskuu",
        r"tammi-joulukuu",
        # Swedish
        r"delårsrapport",
        r"halvårsrapport",
        r"kvartalsrapport",
        r"bokslutskommuniké",
        r"bokslut",
        r"årsredovisning",
        r"delårsredogörelse",
        r"januari-mars",
        r"januari-juni",
        r"januari-september",
        r"januari-december",
        # English / Common codes
        r"interim\s+(?:financial\s+)?report",
        r"half-year\s+(?:financial\s+)?report",
        r"quarterly\s+(?:financial\s+)?report",
        r"financial\s+statement(?:s|\s+release)?",
        r"annual\s+report",
        r"earnings\s+release",
        r"results\s+for\s+the\s+period",
        r"\bq[1-4]\s*[-/]?\s*20\d\d\b",
        r"\bh[1-2]\s*[-/]?\s*20\d\d\b",
        r"\bq[1-4]-rapport\b",
    ]

    for pat in core_patterns:
        if re.search(pat, text_to_check):
            return StrategyType.CORE.value

    # If the release has an attached report PDF and contains financial table/period keywords
    if has_pdf:
        if any(re.search(pat, text_to_check) for pat in [
            r"tulos|tulokse", r"katsaus", r"raport", r"rapport", r"report", r"result",
            r"earning", r"financial", r"bokslut", r"tilinpäätös", r"vuosi", r"quarter", r"h[1-2]", r"q[1-4]"
        ]) or not text_to_check.strip():
            return StrategyType.CORE.value

    # 3. SATELLITE (Catalysts, Insider buying, Profit warnings, Orders, PRs)
    return StrategyType.SATELLITE.value


def classify_release_strategy(
    title: str,
    category: Optional[str] = None,
    has_pdf: bool = False,
    full_text: str = "",
) -> str:
    """Backward-compatible wrapper for route_document."""
    return route_document(news_item=title, has_pdf=has_pdf, full_text=full_text)



def chunk_text(text: str, max_words: int = 1500) -> List[str]:
    """
    Splits long unstructured text (press releases, PDF reports) into
    manageable chunks based on word count to protect the LLM context window.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= max_words:
        return [text]

    chunks = []
    for i in range(0, len(words), max_words):
        chunks.append(" ".join(words[i : i + max_words]))

    logger.debug(f"Chunked document ({len(words)} words) into {len(chunks)} chunks of max {max_words} words.")
    return chunks


def extract_json_from_llm_response(raw_text: str) -> Dict[str, Any]:
    """Safely extract and parse JSON from LLM text, stripping markdown code fences if present."""
    if not raw_text or not raw_text.strip():
        raise json.JSONDecodeError("Empty LLM response", "", 0)

    cleaned = raw_text.strip()

    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()

    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        cleaned = cleaned[start_idx : end_idx + 1]

    return json.loads(cleaned)


def _call_openrouter_api(
    prompt: str,
    system_prompt: str,
    api_key: str,
    model: str,
    max_retries: int = MAX_RETRIES,
) -> Optional[Dict[str, Any]]:
    """Internal helper to invoke OpenRouter API with rate-limiting backoff."""
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "HTTP-Referer": "https://github.com/tradeBotTiuku/screener",
        "X-Title": "Nasdaq Helsinki MicroCap Screener",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=60.0)
            if response.status_code in (401, 402, 403, 404):
                if response.status_code == 402:
                    # 402 on free-tier model = daily request quota exhausted.
                    # Account has sufficient credit ($10+); this is purely a rate cap on the
                    # free model (meta-llama/llama-3-8b-instruct:free). Resets after ~24h.
                    # Retrying won't help — fall back to rule-based logic for this cycle.
                    logger.warning(
                        f"[LLM DEGRADED MODE] OpenRouter HTTP 402 — daily free-tier quota "
                        f"exhausted for {os.getenv('OPENROUTER_MODEL', DEFAULT_OPENROUTER_MODEL)}. "
                        "Falling back to rule-based (context-free) logic for this cycle. "
                        "Quota resets automatically after ~24h."
                    )
                else:
                    logger.warning(f"OpenRouter API returned HTTP {response.status_code}. Using fallback.")
                return None

            if response.status_code == 429 or 500 <= response.status_code < 600:
                backoff = RETRY_BACKOFF_DELAYS[attempt - 1] if attempt - 1 < len(RETRY_BACKOFF_DELAYS) else RETRY_BACKOFF_DELAYS[-1]
                logger.warning(f"OpenRouter returned HTTP {response.status_code} (attempt {attempt}/{max_retries}). Backing off {backoff}s...")
                time.sleep(backoff)
                continue

            response.raise_for_status()
            data = response.json()
            
            # Record Token Usage
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if prompt_tokens > 0 or completion_tokens > 0:
                try:
                    from core.token_tracker import record_tokens
                    record_tokens(prompt_tokens, completion_tokens, model=model, source="nlp_analyzer")
                except Exception:
                    pass

            choices = data.get("choices", [])
            if not choices:
                return None
            raw_output = choices[0].get("message", {}).get("content", "")
            return extract_json_from_llm_response(raw_output)

        except (requests.exceptions.RequestException, json.JSONDecodeError) as err:
            backoff = RETRY_BACKOFF_DELAYS[attempt - 1] if attempt - 1 < len(RETRY_BACKOFF_DELAYS) else RETRY_BACKOFF_DELAYS[-1]
            logger.warning(f"OpenRouter error on attempt {attempt}: {err}. Retrying in {backoff}s...")
            time.sleep(backoff)

    return None


def analyze_text(
    text_content: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_words: int = 1500,
    analyze_first_chunk_only: bool = True,
    throttle_sleep_seconds: float = DEFAULT_THROTTLE_SLEEP,
    strategy_type: str = "SATELLITE",
) -> Dict[str, Any]:
    """
    Main entry point for LLM analysis. Dispatches to Satellite or Core analyzer.
    """
    if strategy_type.upper() == StrategyType.CORE.value:
        return analyze_core_fundamentals(
            text_content=text_content,
            api_key=api_key,
            model=model,
            max_words=max_words,
            throttle_sleep_seconds=throttle_sleep_seconds,
        )
    else:
        return analyze_satellite_catalysts(
            text_content=text_content,
            api_key=api_key,
            model=model,
            max_words=max_words,
            analyze_first_chunk_only=analyze_first_chunk_only,
            throttle_sleep_seconds=throttle_sleep_seconds,
        )


def analyze_satellite_catalysts(
    text_content: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_words: int = 1500,
    analyze_first_chunk_only: bool = True,
    throttle_sleep_seconds: float = DEFAULT_THROTTLE_SLEEP,
) -> Dict[str, Any]:
    """Analyzes daily releases for catalyst events (MAR personal buys, guidance upgrades)."""
    key = api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY")
    if not key:
        return rule_based_analyze_text(text_content)

    selected_model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    chunks = chunk_text(text_content, max_words=max_words)
    if not chunks:
        return dict(SAFE_DEFAULT_RESPONSE)

    target_chunks = [chunks[0]] if analyze_first_chunk_only else chunks
    aggregated_result = dict(SAFE_DEFAULT_RESPONSE)
    reasons: List[str] = []

    for chunk in target_chunks:
        parsed_json = _call_openrouter_api(
            prompt=f"Company Announcement Content:\n{chunk}",
            system_prompt=SATELLITE_SYSTEM_PROMPT,
            api_key=key,
            model=selected_model,
        )
        if not parsed_json:
            fallback = rule_based_analyze_text(chunk)
            aggregated_result["insider_buying_personal"] |= fallback["insider_buying_personal"]
            aggregated_result["company_share_buyback"] |= fallback["company_share_buyback"]
            aggregated_result["cash_issue"] |= fallback["cash_issue"]
            aggregated_result["positive_guidance"] |= fallback["positive_guidance"]
            aggregated_result["management_buying"] |= fallback["management_buying"]
            if fallback.get("reasoning"):
                reasons.append(fallback["reasoning"])
        else:
            buyback = bool(parsed_json.get("company_share_buyback", False))
            personal_buying = False if buyback else bool(
                parsed_json.get("insider_buying_personal", parsed_json.get("management_buying", False))
            )
            cash_issue = bool(parsed_json.get("cash_issue", False))
            pos_guidance = bool(parsed_json.get("positive_guidance", False))
            reason_text = str(parsed_json.get("reasoning", "")).strip()

            aggregated_result["insider_buying_personal"] |= personal_buying
            aggregated_result["company_share_buyback"] |= buyback
            aggregated_result["cash_issue"] |= cash_issue
            aggregated_result["positive_guidance"] |= pos_guidance
            aggregated_result["management_buying"] |= personal_buying
            if reason_text:
                reasons.append(reason_text)

        if throttle_sleep_seconds > 0:
            time.sleep(throttle_sleep_seconds)

    if reasons:
        aggregated_result["reasoning"] = " ".join(reasons)
    else:
        aggregated_result["reasoning"] = generate_fallback_rationale(
            aggregated_result["insider_buying_personal"],
            aggregated_result["company_share_buyback"],
            aggregated_result["cash_issue"],
            aggregated_result["positive_guidance"],
        )

    return aggregated_result


def analyze_core_fundamentals(
    text_content: str,
    ticker: Optional[str] = None,
    hard_financials: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_words: int = 2000,
    throttle_sleep_seconds: float = DEFAULT_THROTTLE_SLEEP,
) -> Dict[str, Any]:
    """Analyzes earnings and annual reports with dual-lens (Profile A Growth / Profile B Value)."""
    # Deterministic hard financials retrieval if ticker provided and not pre-supplied
    if hard_financials is None and ticker:
        try:
            from .financial_metrics_engine import get_hard_financials
            hard_financials = get_hard_financials(ticker)
        except Exception as e:
            logger.debug(f"Could not retrieve hard financials for {ticker}: {e}")

    key = api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY")
    if not key:
        return rule_based_analyze_core_fundamentals(text_content)

    selected_model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    chunks = chunk_text(text_content, max_words=max_words)
    if not chunks:
        return dict(SAFE_DEFAULT_CORE_RESPONSE)

    # Use first 2 chunks for earnings reports to capture financial summaries & table highlights
    target_text = "\n\n".join(chunks[:2])

    facts_block = ""
    if hard_financials:
        hard_facts_text = format_for_llm_prompt(hard_financials)
        facts_block = (
            f"[HARD FINANCIAL FACTS - DO NOT RECALCULATE]\n"
            f"{hard_facts_text}\n\n"
            "INSTRUCTION: Treat the provided Hard Financial Facts as absolute truth. "
            "Base your 'Section 1' survival analysis strictly on these provided numbers, "
            "and use the text document ONLY for qualitative context (management commentary, restructurings, M&A).\n\n"
        )

    user_prompt = f"{facts_block}Financial Report & Financial Statements Content:\n{target_text}"

    parsed_json = _call_openrouter_api(
        prompt=user_prompt,
        system_prompt=CORE_SYSTEM_PROMPT,
        api_key=key,
        model=selected_model,
    )

    if throttle_sleep_seconds > 0:
        time.sleep(throttle_sleep_seconds)

    if not parsed_json:
        return rule_based_analyze_core_fundamentals(target_text)

    # Validate / Normalize dual-lens response
    prof_a = parsed_json.get("profile_A_growth", {})
    prof_b = parsed_json.get("profile_B_value", {})
    safety = parsed_json.get("financial_safety", {})
    verdict_info = parsed_json.get("verdict_details", {})
    flags = parsed_json.get("flags", {})
    metrics = parsed_json.get("metrics", {})

    gm_val = metrics.get("gross_margin_pct")
    gm_over_40 = bool(prof_a.get("gross_margin_over_40", False)) or (gm_val is not None and gm_val >= 40.0)
    r40_passed = bool(prof_a.get("rule_of_40_passed", False))
    recurring = bool(prof_a.get("recurring_revenue_mentioned", False))
    organic_growth = bool(prof_a.get("organic_growth_confirmed", flags.get("organic_growth_confirmed", True)))

    net_cash = bool(prof_b.get("strong_net_cash_position", False)) or bool(metrics.get("net_cash_positive", False))
    pos_ocf = bool(prof_b.get("positive_operating_cash_flow", False))
    turnaround = bool(prof_b.get("turnaround_indicators", False))
    rev_shrinking = bool(prof_b.get("revenue_shrinking", False)) or bool(flags.get("shrinking_business", False))

    going_concern = bool(safety.get("going_concern_risk", parsed_json.get("cash_issue", False)))
    dilution_risk = bool(safety.get("dilution_risk_detected", flags.get("dilution_risk_detected", False)))
    unsustainable_cash_burn = bool(safety.get("unsustainable_cash_burn", flags.get("unsustainable_cash_burn", False)))
    erratic_pivots = bool(safety.get("erratic_pivots_detected", flags.get("erratic_pivots_detected", False)))

    safety_failed = going_concern or dilution_risk or unsustainable_cash_burn or erratic_pivots

    # Extract Profile & Verdict
    raw_profile = str(parsed_json.get("profile", verdict_info.get("matched_profile", ""))).strip().upper()
    if "PROFILE_A" in raw_profile or "GROWTH" in raw_profile:
        matched_profile = "GROWTH"
    elif "PROFILE_B" in raw_profile or "VALUE" in raw_profile:
        matched_profile = "VALUE"
    else:
        matched_profile = "NONE"

    verdict = str(parsed_json.get("verdict", verdict_info.get("verdict", ""))).strip().upper()

    if safety_failed:
        matched_profile = "NONE"
        verdict = "REJECT"
    elif matched_profile == "NONE":
        if gm_over_40 and r40_passed and recurring and organic_growth:
            matched_profile = "GROWTH"
            verdict = "STRONG BUY"
        elif net_cash and pos_ocf and turnaround and not rev_shrinking:
            matched_profile = "VALUE"
            verdict = "STRONG BUY"
        elif not verdict:
            verdict = "HOLD"

    # Strict Anti-Shrinking Disqualification
    if rev_shrinking and not turnaround:
        if matched_profile == "VALUE" and verdict == "STRONG BUY":
            verdict = "HOLD"
            matched_profile = "NONE"

    if safety_failed:
        verdict = "REJECT"
    elif verdict not in ("STRONG BUY", "HOLD", "REJECT", "WATCH_TURNAROUND"):
        verdict = "STRONG BUY" if matched_profile in ("GROWTH", "VALUE") else "HOLD"

    reasoning = str(parsed_json.get("pedagogical_reasoning", verdict_info.get("reasoning", parsed_json.get("reasoning", "")))).strip()
    if not reasoning:
        reasoning = generate_core_fallback_rationale(
            going_concern_risk=going_concern,
            dilution_risk=dilution_risk,
            unsustainable_cash_burn=unsustainable_cash_burn,
            erratic_pivots=erratic_pivots,
            gross_margin_over_40=gm_over_40,
            rule_of_40_passed=r40_passed,
            recurring_revenue_mentioned=recurring,
            organic_growth_confirmed=organic_growth,
            strong_net_cash_position=net_cash,
            positive_operating_cash_flow=pos_ocf,
            turnaround_indicators=turnaround,
            revenue_shrinking=rev_shrinking,
            matched_profile=matched_profile,
            verdict=verdict,
            prof_a={
                "gross_margin_over_40": gm_over_40,
                "rule_of_40_passed": r40_passed,
                "recurring_revenue_mentioned": recurring,
                "organic_growth_confirmed": organic_growth,
            },
            prof_b={
                "strong_net_cash_position": net_cash,
                "positive_operating_cash_flow": pos_ocf,
                "turnaround_indicators": turnaround,
            },
        )

    # Core quality passed if either Profile A or Profile B satisfies STRONG BUY
    quality_passed = (verdict == "STRONG BUY" and not safety_failed)

    return {
        "profile_A_growth": {
            "gross_margin_over_40": gm_over_40,
            "rule_of_40_passed": r40_passed,
            "recurring_revenue_mentioned": recurring,
            "organic_growth_confirmed": organic_growth,
        },
        "profile_B_value": {
            "strong_net_cash_position": net_cash,
            "positive_operating_cash_flow": pos_ocf,
            "turnaround_indicators": turnaround,
        },
        "financial_safety": {
            "going_concern_risk": going_concern,
            "dilution_risk_detected": dilution_risk,
            "unsustainable_cash_burn": unsustainable_cash_burn,
            "erratic_pivots_detected": erratic_pivots,
        },
        "verdict_details": {
            "matched_profile": matched_profile,
            "verdict": verdict,
            "reasoning": reasoning,
        },
        # Backwards compatibility flat helpers
        "cash_issue": safety_failed,
        "gross_margin_above_40": gm_over_40,
        "recurring_revenue": recurring,
        "rule_of_40_passed": r40_passed,
        "core_quality_passed": quality_passed,
        "matched_profile": matched_profile,
        "verdict": verdict,
        "reasoning": reasoning,
    }


def generate_fallback_rationale(
    insider_buying_personal: bool,
    company_share_buyback: bool,
    cash_issue: bool,
    positive_guidance: bool,
) -> str:
    """Generates structured educational Finnish explanation for Satellite trades."""
    if cash_issue:
        return (
            "Tämä yhtiö hylättiin koska (a) tiedotteessa havaittiin merkittäviä maksuvalmius- tai käyttöpääomarismejä, "
            "(b) toiminnan jatkuvuuteen liittyy epävarmuutta, (c) lisärahoituksen tarve uhkaa aiheuttaa diluutiota."
        )
    if insider_buying_personal:
        return (
            "Tämä yhtiö nousi listalle koska (a) avainjohtaja osti osakkeita henkilökohtaisella pääomallaan avoimilta markkinoilta, "
            "(b) suora sisäpiiriosto viestii johdon vahvasta sitoutumisesta ja näkymien aliarvostuksesta, "
            "(c) riskihuomio: tarkista kassa ja tase jotta kyseessä ei ole vain kurssituki."
        )
    if positive_guidance:
        return (
            "Tämä yhtiö nousi listalle koska (a) yhtiö antoi positiivisen tulosvaroituksen tai nosti ohjeistustaan vahvan kysynnän ansiosta, "
            "(b) operatiivinen tuloskäänne ylittää odotukset, "
            "(c) riskihuomio: varmista liikevaihdon ja tuloksen samanaikainen kasvu."
        )
    if company_share_buyback:
        return (
            "Tämä yhtiö jätettiin neutraaliksi koska (a) kyseessä on yhtiön oma omien osakkeiden takaisinosto-ohjelma, "
            "(b) kyse ei ole yksittäisen johtohenkilön omalla riskillään tekemästä ostosta."
        )
    return (
        "Tämä yhtiö jätettiin neutraaliksi koska (a) tiedote on luonteeltaan rutiininomainen, "
        "(b) tiedotteesta ei löytynyt selkeitä kurssiajureita kuten sisäpiirin ostoja tai ohjeistusnostoja."
    )


def generate_core_fallback_rationale(
    going_concern_risk: bool,
    matched_profile: str,
    verdict: str,
    prof_a: Dict[str, bool],
    prof_b: Dict[str, bool],
    dilution_risk: bool = False,
    unsustainable_cash_burn: bool = False,
    erratic_pivots: bool = False,
) -> str:
    """Generates structured Finnish pedagogical explanation for Dual-Lens Core analysis."""
    if going_concern_risk:
        return (
            "Tämä yhtiö hylättiin Core-salkusta riskiseulan perusteella: (a) Raportissa havaittiin vakava toiminnan jatkuvuuden riski, "
            "käyttöpääomakriisi tai kovenanttirikko. (b) Riskiseula ensin: taseriski kumoaa kaiken tuotto-odotuksen."
        )
    if verdict == "WARN":
        return (
            "Yhtiölle asetettu VAROITUSTILA (WARN - Dual Confirmation): (a) Havaittu pääomarakenne- tai diluutiomuutos "
            "(kuten käänteinen split pörssilistauksen turvaamiseksi tai lyhyt kassapuskuri), mutta yhtiöllä on liiketoimintaa tai "
            "käänne-elementtejä. (b) Edellyttää hintatason heikkenemisen vahvistusta ennen mahdollista irtautumista."
        )
    if dilution_risk:
        return (
            "Tämä yhtiö hylättiin Core-salkusta diluutio- ja pääomatuhoriskin perusteella: (a) Havaittu käänteinen split (reverse stock split), "
            "jatkuva massiivinen osakeanti tai arvoa tuhoavat konvertoitavat lainat."
        )
    if unsustainable_cash_burn:
        return (
            "Tämä yhtiö hylättiin Core-salkusta kestämättömän kassapolton vuoksi: (a) Negatiivinen liiketoiminnan rahavirta kuluttaa kassavarat alle 4 kvartaalissa (< 12 kk runway) "
            "ilman näkymää kannattavuudesta tai ei-dilutoivasta rahoituksesta."
        )
    if erratic_pivots:
        return (
            "Tämä yhtiö hylättiin Core-salkusta epäjohdonmukaisten strategiakäännösten vuoksi: (a) Yhtiö on muuttanut liiketoimintamalliaan radikaalisti täysin toisille toimialoille "
            "(esim. opportunistiset käännökset krypto-/tekoälykupliin)."
        )
    if verdict == "STRONG BUY":
        if matched_profile == "GROWTH":
            return (
                "Valittu Core-salkkuun Kasvu-profiilin (Profile A) mukaisesti: (1) Korkea skaalautuva bruttokate (> 40%), toistuva SaaS/sopimusliikevaihto ja vahvistettu orgaaninen kasvu. "
                "(2) Vahva Rule of 40 kasvuvauhti ja operatiivinen vipu ilman taseriskejä tai diluutiota."
            )
        elif matched_profile == "VALUE":
            return (
                "Valittu Core-salkkuun Arvo/Käänne-profiilin (Profile B) mukaisesti: (1) Vahva nettovelaton kassapuskuri ja positiivinen operatiivinen rahavirta. "
                "(2) Selkeät tehostumisen ja tuloskäänteen merkit houkuttelevalla arvostuksella."
            )
    return (
        f"Jätetty odottamaan (verdict: {verdict}): Yhtiö ei täytä riittävästi Profile A (Kasvu) eikä Profile B (Arvo/Käänne) vaatimuksia, "
        f"vaikka välitöntä maksukyvyttömyys- tai diluutioriskiä ei havaittu."
    )


def rule_based_analyze_text(text: str) -> Dict[str, Any]:
    """Deterministic rule-based keyword matcher for Satellite signals."""
    if not text:
        return dict(SAFE_DEFAULT_RESPONSE)

    lower = text.lower()

    # 1. Cash Issue detection
    cash_issue = False
    cash_patterns = [
        r"kassavarantojen riittävyys",
        r"tarvitsee lisärahoitusta",
        r"käyttöpääoma ei riitä",
        r"käyttöpääoma on loppunut",
        r"maksuvalmius on (?:merkittävästi )?heikentynyt",
        r"maksuvalmiuden turvaamiseksi",
        r"yrityssaneerau(?:ksen|kseen)",
        r"toiminnan jatkuvuuteen liittyy merkittävää epävarmuutta",
        r"going concern",
        r"material uncertainty related to going concern",
        r"covenant.*breach|kovenantti.*rikko",
        r"likviditeettiasema on heikentynyt",
        r"likviditeettikriisi",
    ]
    for pattern in cash_patterns:
        if re.search(pattern, lower):
            cash_issue = True
            break

    # Positive runway override
    pos_runway = re.search(r"kassa(?:varat)? riittävät.*?(\d+)\s*kuukaut", lower)
    if pos_runway:
        try:
            if int(pos_runway.group(1)) >= 18:
                cash_issue = False
        except ValueError:
            pass

    # 2. Company share buyback detection
    company_buyback = any(
        k in lower
        for k in [
            "omien osakkeiden hankinta",
            "omien osakkeiden hankintaan",
            "omien osakkeiden osto",
            "acquisition of own shares",
            "repurchase of own shares",
            "share buy-back",
            "share buyback",
        ]
    )

    # 3. Personal Insider Buying
    insider_buying_personal = False
    if not company_buyback:
        is_mar = any(
            k in lower
            for k in [
                "johdon liiketoimet",
                "johtohenkilöiden liiketoimet",
                "johtoryhmän jäsen",
                "toimitusjohtaja",
                "hallituksen jäsen",
                "hallituksen puheenjohtaja",
                "managers' transactions",
                "manager transaction",
                "insider",
                "sisäpiiri",
            ]
        )
        has_buy = any(
            k in lower
            for k in [
                "hankinta",
                "hankkinut",
                "acquisition",
                "merkintä",
                "subscription",
                "ostanut",
                "ostos",
                "bought",
                "purchased",
            ]
        )
        if is_mar and has_buy:
            insider_buying_personal = True

    # 4. Positive Guidance detection
    pos_guidance = False
    guidance_upgrade_patterns = [
        r"positiivinen\s+tulosvaroitus",
        r"nostaa\s+.*?ohjeistus",
        r"nostaa\s+.*?näkymiään",
        r"parantaa\s+.*?ohjeistus",
        r"parantaa\s+.*?näkymiään",
        r"raises?\s+.*?guidance",
        r"upgrades?\s+.*?outlook",
    ]
    for pattern in guidance_upgrade_patterns:
        if re.search(pattern, lower):
            pos_guidance = True
            break

    if re.search(r"negatiivinen\s+tulosvaroitus|laskee\s+.*?ohjeistus|lowers?\s+.*?guidance", lower):
        pos_guidance = False

    reasoning = generate_fallback_rationale(insider_buying_personal, company_buyback, cash_issue, pos_guidance)

    return {
        "insider_buying_personal": insider_buying_personal,
        "company_share_buyback": company_buyback,
        "cash_issue": cash_issue,
        "positive_guidance": pos_guidance,
        "management_buying": insider_buying_personal,
        "reasoning": reasoning,
    }


def rule_based_analyze_core_fundamentals(text: str) -> Dict[str, Any]:
    """Deterministic rule-based keyword & financial metric matcher for Dual-Lens Core fundamentals."""
    if not text:
        return dict(SAFE_DEFAULT_CORE_RESPONSE)

    lower = text.lower()

    # 1. Financial Safety / Going Concern Risk
    going_concern = False
    for pat in [
        r"toiminnan jatkuvuuteen liittyy",
        r"käyttöpääoma ei riitä",
        r"tarvitsee lisärahoitusta",
        r"negatiivinen oma pääoma",
        r"hätälaina",
        r"maksuvalmius on heikentynyt",
        r"going concern",
        r"kovenanttirikko",
        r"kontrollbalansräkning",
        r"rekonstruktion",
        r"yrityssaneeraus",
    ]:
        if re.search(pat, lower):
            # Negation check: ignore if explicitly stated NOT required / false alarm
            negation_pats = [
                r"ei\s+(?:ole\s+)?(?:velvoitettu\s+)?(?:tarvetta\s+)?(?:tarpeen\s+)?(?:laatia\s+)?kontrollbalansräkning",
                r"inte\s+(?:behöver\s+)?upprätta\s+kontrollbalansräkning",
                r"ei\s+tarvetta\s+(?:yritys)?saneeraukselle",
                r"inte\s+aktuellt\s+med\s+rekonstruktion",
                r"no\s+going\s+concern\s+risk",
            ]
            if any(re.search(np, lower) for np in negation_pats):
                continue
            going_concern = True
            break

    # 1b. Dilution Risk / Reverse Split Detection
    dilution_risk = False
    for pat in [
        r"reverse stock split",
        r"reverse split",
        r"share consolidation",
        r"osakkeiden yhdistäminen",
        r"käänteinen split",
        r"continuous dilution",
        r"death spiral",
        r"toxic convertible",
        r"at-the-market",
        r"atm\s+(?:equity\s+)?(?:dilution\s+)?program",
        r"atm\s+offering",
        r"equity dilution program",
        r"företrädesemission",
        r"riktad nyemission",
        r"nyemission",
        r"aktieemission",
        r"suunnattu anti",
        r"osakeanti",
    ]:
        if re.search(pat, lower):
            # Negation/completion check: ignore if terminated, closed, or cancelled
            negation_atm = [
                r"atm[^\.\n]{0,50}(?:terminated|cancelled|closed|completed|ei ole aktiivinen|lopetettu|päätetty)",
                r"(?:terminated|cancelled|closed)[^\.\n]{0,50}atm",
            ]
            if any(re.search(na, lower) for na in negation_atm):
                continue
            dilution_risk = True
            break

    # 1c. Unsustainable Cash Burn Detection (< 4 quarters runway)
    unsustainable_cash_burn = False
    for pat in [
        r"kassavarat eivät riitä 12",
        r"kassavarat riittävät alle",
        r"cash runway (?:of )?less than",
        r"runway is less than 12",
        r"depleted within 12 months",
        r"depleted within 4 quarters",
        r"insufficient liquidity for the next 12",
    ]:
        if re.search(pat, lower):
            unsustainable_cash_burn = True
            break

    # 1d. Erratic Strategic Pivots
    erratic_pivots = False
    for pat in [
        r"pivot to crypto",
        r"pivot to ai",
        r"pivot to blockchain",
        r"pivot to bitcoin",
        r"changed business model to crypto",
        r"reverse merger with",
    ]:
        if re.search(pat, lower):
            erratic_pivots = True
            break

    safety_failed = going_concern or dilution_risk or unsustainable_cash_burn or erratic_pivots

    # 2. Profile A (Growth) Indicators
    recurring_revenue = any(
        k in lower
        for k in [
            "toistuva liikevaihto",
            "jatkuva liikevaihto",
            "jatkuvalaskutteinen",
            "saas",
            "tilauspohjainen",
            "recurring revenue",
            "arr",
            "mrr",
            "tilaussopimukset",
            "ylläpitosopimukset",
            "palvelusopimukset",
            "subscription",
            "enterprise adoption",
        ]
    )

    organic_growth = not any(
        k in lower
        for k in [
            "non-organic growth",
            "acquisition of shell",
            "acquired dormant entity",
            "one-off non-operating gain",
        ]
    )

    gm_pct = 0.0
    gm_over_40 = False
    gm_match = re.search(r"(?:myyntikate|bruttokate|gross margin)[^\d%]{0,30}(\d+[\.,]?\d*)\s*%", lower)
    if gm_match:
        try:
            gm_pct = float(gm_match.group(1).replace(",", "."))
            gm_over_40 = gm_pct >= 40.0
        except ValueError:
            pass
    elif recurring_revenue or "ohjelmisto" in lower or "software" in lower or "lisenssi" in lower:
        gm_pct = 65.0
        gm_over_40 = True

    rev_growth = 0.0
    growth_match = re.search(r"(?:liikevaihto kasvoi|liikevaihdon kasvu|liikevaihto nousi|revenue growth|revenue of .*? up|revenue was .*? up|up\s+)[^\d%]{0,30}(\d+[\.,]?\d*)\s*%", lower)
    if growth_match:
        try:
            rev_growth = float(growth_match.group(1).replace(",", "."))
        except ValueError:
            rev_growth = 0.0
    elif re.search(r"kasvoi\s+(?:[2-9]\d|1\d\d)\s*%", lower) or "vahva kasvu" in lower or "record quarterly revenue" in lower:
        rev_growth = 30.0

    op_margin = 0.0
    ebit_match = re.search(r"(?:liikevoittomarginaali|liikevoitto-%|ebit-%|ebit margin)[^\d%]{0,30}([+-]?\d+[\.,]?\d*)\s*%", lower)
    if ebit_match:
        try:
            op_margin = float(ebit_match.group(1).replace(",", "."))
        except ValueError:
            op_margin = 0.0

    r40_score = rev_growth + op_margin
    r40_passed = (r40_score >= 40.0) or (rev_growth >= 20.0 and (op_margin >= 10.0 or recurring_revenue or gm_over_40))

    # 3. Profile B (Deep Value / Turnaround) Indicators
    net_cash = any(
        k in lower
        for k in [
            "velaton",
            "nettovelaton",
            "net cash",
            "vahva kassa",
            "kassavarat ylittävät velat",
            "vahva tase",
            "kassavarat olivat",
            "nettokassa (kassa - velka):",
        ]
    )
    pos_ocf = any(
        k in lower
        for k in [
            "liiketoiminnan rahavirta oli positiivinen",
            "operatiivinen rahavirta oli positiivinen",
            "positiivinen rahavirta",
            "positive cash flow",
            "operating cash flow was positive",
            "rahavirta parani",
            "operatiivinen kassavirta (ttm/viimeisin):",
        ]
    )
    turnaround = any(
        k in lower
        for k in [
            "käänne",
            "tuloskäänne",
            "tehostamisohjelma",
            "säästöohjelma",
            "kannattavuus parani merkittävästi",
            "palasi voitolliseksi",
            "restructuring",
            "turnaround",
            "kannattavuuskäänteen",
            "strategisen lisensointisopimuksen",
            "johdon liiketoimet",
        ]
    )

    # 0. Administrative & Routine Calendar Notice check
    is_routine_calendar = any(
        k in lower
        for k in [
            "julkaisee taloudelliset katsauksensa vuonna",
            "taloudellinen kalenteri",
            "financial calendar",
            "finansiell kalender",
            "yhtiökokous pidetään",
            "julkistamisajankohdat",
        ]
    )

    fatal_safety_failed = going_concern or erratic_pivots
    warning_safety_detected = dilution_risk or unsustainable_cash_burn

    if fatal_safety_failed:
        matched_profile = "NONE"
        verdict = "REJECT"
    elif warning_safety_detected:
        if turnaround or gm_over_40 or rev_growth > 0 or net_cash or pos_ocf:
            matched_profile = "WATCH_TURNAROUND" if turnaround else "VALUE"
            verdict = "WARN"
        else:
            matched_profile = "NONE"
            verdict = "REJECT"
    elif is_routine_calendar:
        matched_profile = "NONE"
        verdict = "HOLD"
    elif ("johdon liiketoimet" in lower or "kannattavuuskäänteen" in lower or "turnaround" in lower) and (net_cash or pos_ocf):
        matched_profile = "VALUE"
        verdict = "WATCH_TURNAROUND"
    elif (gm_over_40 or rev_growth >= 25.0) and r40_passed and organic_growth:
        matched_profile = "GROWTH"
        verdict = "STRONG BUY"
    elif net_cash and (pos_ocf or turnaround):
        matched_profile = "VALUE"
        verdict = "STRONG BUY"
    else:
        matched_profile = "NONE"
        verdict = "HOLD"

    prof_a = {
        "gross_margin_over_40": gm_over_40,
        "rule_of_40_passed": r40_passed,
        "recurring_revenue_mentioned": recurring_revenue,
        "organic_growth_confirmed": organic_growth,
    }
    prof_b = {
        "strong_net_cash_position": net_cash,
        "positive_operating_cash_flow": pos_ocf,
        "turnaround_indicators": turnaround,
    }
    safety_dict = {
        "going_concern_risk": going_concern,
        "dilution_risk_detected": dilution_risk,
        "unsustainable_cash_burn": unsustainable_cash_burn,
        "erratic_pivots_detected": erratic_pivots,
        "warning_detected": warning_safety_detected,
    }

    reasoning = generate_core_fallback_rationale(
        going_concern_risk=going_concern,
        dilution_risk=dilution_risk,
        unsustainable_cash_burn=unsustainable_cash_burn,
        erratic_pivots=erratic_pivots,
        matched_profile=matched_profile,
        verdict=verdict,
        prof_a=prof_a,
        prof_b=prof_b,
    )

    quality_passed = (verdict == "STRONG BUY" and not fatal_safety_failed)

    return {
        "profile_A_growth": prof_a,
        "profile_B_value": prof_b,
        "financial_safety": safety_dict,
        "verdict_details": {
            "matched_profile": matched_profile,
            "verdict": verdict,
            "reasoning": reasoning,
        },
        # Backwards compatibility flat helpers
        "cash_issue": safety_failed,
        "gross_margin_pct": gm_pct,
        "gross_margin_above_40": gm_over_40,
        "revenue_growth_pct": rev_growth,
        "operating_margin_pct": op_margin,
        "rule_of_40_score": r40_score,
        "recurring_revenue": recurring_revenue,
        "rule_of_40_passed": r40_passed,
        "core_quality_passed": quality_passed,
        "matched_profile": matched_profile,
        "verdict": verdict,
        "reasoning": reasoning,
    }


class FinancialNLPAnalyzer:
    """
    High-level analyzer integrating OpenRouter API and rule-based fallbacks
    with the screener pipeline's ScrapedRelease data models and router.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        config: Optional[ScreenerConfig] = None,
    ):
        self.config = config or ScreenerConfig.from_env()
        if api_key is not None:
            self.api_key = api_key
        else:
            self.api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)

    def analyze_release(self, release: ScrapedRelease) -> NLPExtractionResult:
        """
        Routes release to either Core or Satellite NLP analysis and returns structured result.
        """
        doc_text = release.document.full_combined_text.strip()
        header_parts = [release.feed_item.title]
        if release.feed_item.category:
            header_parts.append(release.feed_item.category)
        if release.feed_item.summary:
            header_parts.append(release.feed_item.summary)

        header_text = "\n".join(header_parts)
        full_text = f"{header_text}\n\n{doc_text}".strip() if doc_text else header_text
        has_pdf = len(release.document.pdf_urls) > 0 or bool(release.document.pdf_text.strip())

        # Determine strategy route
        strat = classify_release_strategy(
            title=release.feed_item.title,
            category=release.feed_item.category,
            has_pdf=has_pdf,
            full_text=full_text,
        )

        throttle = 0.0 if not self.api_key else DEFAULT_THROTTLE_SLEEP

        if strat == StrategyType.CORE.value:
            signals = analyze_core_fundamentals(
                text_content=full_text,
                ticker=release.feed_item.ticker,
                api_key=self.api_key,
                model=self.model,
                throttle_sleep_seconds=throttle,
            )
            return NLPExtractionResult(
                release_id=release.id,
                ticker=release.feed_item.ticker,
                company_name=release.feed_item.company_name,
                strategy_type=StrategyType.CORE.value,
                has_cash_flow_issues=signals.get("cash_issue", False),
                funding_need_explanation="Riskiseula: Tase- tai maksuvalmiusongelma" if signals.get("cash_issue") else "",
                gross_margin_pct=signals.get("gross_margin_pct"),
                gross_margin_above_40=signals.get("gross_margin_above_40", False),
                recurring_revenue=signals.get("recurring_revenue", False),
                recurring_revenue_details=signals.get("recurring_revenue_details", ""),
                revenue_growth_pct=signals.get("revenue_growth_pct"),
                operating_margin_pct=signals.get("operating_margin_pct"),
                rule_of_40_score=signals.get("rule_of_40_score"),
                rule_of_40_passed=signals.get("rule_of_40_passed", False),
                core_quality_passed=signals.get("core_quality_passed", False),
                educational_rationale=signals.get("reasoning", ""),
                confidence_score=0.90 if self.api_key else 0.75,
                raw_llm_response=json.dumps(signals),
            )
        else:
            signals = analyze_satellite_catalysts(
                text_content=full_text,
                api_key=self.api_key,
                model=self.model,
                throttle_sleep_seconds=throttle,
            )
            direction = "BUY" if signals.get("management_buying") else "NONE"
            tx_details = "Management transaction (BUY) detected." if signals.get("management_buying") else ""
            guidance_summary = "Positive guidance upgrade detected." if signals.get("positive_guidance") else ""
            funding_explanation = "Cash flow / liquidity distress signaled." if signals.get("cash_issue") else ""

            return NLPExtractionResult(
                release_id=release.id,
                ticker=release.feed_item.ticker,
                company_name=release.feed_item.company_name,
                strategy_type=StrategyType.SATELLITE.value,
                has_cash_flow_issues=signals.get("cash_issue", False),
                funding_need_explanation=funding_explanation,
                has_management_transactions=signals.get("management_buying", False),
                transaction_direction=direction,
                transaction_details=tx_details,
                is_company_buyback=signals.get("company_share_buyback", False),
                is_positive_profit_warning=signals.get("positive_guidance", False),
                guidance_change_summary=guidance_summary,
                educational_rationale=signals.get("reasoning", ""),
                confidence_score=0.90 if self.api_key else 0.75,
                raw_llm_response=json.dumps(signals),
            )
