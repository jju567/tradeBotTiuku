"""utils/company_names.py - Centralized Company Name Resolution for tradeBotTiuku.

Resolves ticker symbols (e.g. 'MEKKO.HE', 'LIME.ST') into clear, human-readable
company names (e.g. 'Marimekko Oyj', 'Lime Technologies AB') using:
  1. Static dictionary of major Finnish, Nordic, and US instruments.
  2. Universe datasets (nordnet_universe.csv, nordnet_global_universe.csv).
  3. Persistent disk cache (data/company_names_cache.json).
  4. Yahoo Finance API fallback (with automatic caching).
"""

import csv
import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_FILE = DATA_DIR / "company_names_cache.json"

# Static mappings for fast in-memory resolution without disk I/O
STATIC_COMPANY_NAMES: Dict[str, str] = {
    # Helsinki (OMX Helsinki)
    "MEKKO.HE": "Marimekko Oyj",
    "MARIME.HE": "Marimekko Oyj",
    "EQV1V.HE": "eQ Oyj",
    "FARON.HE": "Faron Pharmaceuticals Oy",
    "NOKIA.HE": "Nokia Oyj",
    "SAMPO.HE": "Sampo Oyj",
    "NESTE.HE": "Neste Oyj",
    "KNEBV.HE": "Kone Oyj",
    "FORTUM.HE": "Fortum Oyj",
    "UPM.HE": "UPM-Kymmene Oyj",
    "ELISA.HE": "Elisa Oyj",
    "KESKOB.HE": "Kesko Oyj",
    "VALMT.HE": "Valmet Oyj",
    "WRT1V.HE": "Wärtsilä Oyj",
    "ORNBV.HE": "Orion Oyj",
    "HUH1V.HE": "Huhtamäki Oyj",
    "KEMIRA.HE": "Kemira Oyj",
    "TIETO.HE": "Tietoevry Oyj",
    "STERV.HE": "Stora Enso Oyj",
    "METSA.HE": "Metsä Board Oyj",
    "OUT1V.HE": "Outokumpu Oyj",
    "QTCOM.HE": "Qt Group Oyj",
    "HARVIA.HE": "Harvia Oyj",
    "PUUILO.HE": "Puuilo Oyj",
    "TOKMAN.HE": "Tokmanni Group Oyj",
    "GOFORE.HE": "Gofore Oyj",
    "PON1V.HE": "Ponsse Oyj",
    "TYRES.HE": "Nokian Renkaat Oyj",
    "KAMUX.HE": "Kamux Oyj",
    "KEMPOWR.HE": "Kempower Oyj",
    "ANORA.HE": "Anora Group Oyj",
    "VERK.HE": "Verkkokauppa.com Oyj",
    "VIAFIN.HE": "Viafin Service Oyj",
    "ALMA.HE": "Alma Media Oyj",
    "OLVAS.HE": "Olvi Oyj",
    "KREATE.HE": "Kreate Group Oyj",
    "NOHO.HE": "NoHo Partners Oyj",
    "PIHLIS.HE": "Pihlajalinna Oyj",
    "QPR1V.HE": "QPR Software Oyj",
    "SSH1V.HE": "SSH Communications Security Oyj",
    "ASPO.HE": "Aspo Oyj",
    "DETEC.HE": "Detection Technology Oyj",
    "ENENTO.HE": "Enento Group Oyj",
    "AALLON.HE": "Aallon Group Oyj",
    "ADMCM.HE": "Admicom Oyj",
    "ADMIN.HE": "Administer Oyj",
    "AFAGR.HE": "Afarak Group SE",
    "BITI.HE": "Bittium Oyj",
    "DIGIA.HE": "Digia Oyj",
    "EXEL.HE": "Exel Composites Oyj",
    "HONBS.HE": "Honkarakenne Oyj",
    "ILKKA2.HE": "Ilkka Oyj",
    "INCAP.HE": "Incap Oyj",
    "LINDEX.HE": "Lindex Group Oyj",
    "MARTT.HE": "Martela Oyj",
    "PAMPALO.HE": "Endomines Finland Oyj",
    "RAUTE.HE": "Raute Oyj",
    "RELAS.HE": "Relais Group Oyj",
    "REMED.HE": "Revenio Group Oyj",
    "ROBIT.HE": "Robit Oyj",
    "SCANFL.HE": "Scanfil Oyj",
    "SOLTEQ.HE": "Solteq Oyj",
    "SUOMEC.HE": "Suominen Oyj",
    "TAAL.HE": "Taaleri Oyj",
    "TAMTR.HE": "Tamtron Group Oyj",
    "TITAN.HE": "Titanium Oyj",
    "UNITED.HE": "United Bankers Oyj",
    "VINCIT.HE": "Vincit Oyj",
    "WETTERI.HE": "Wetteri Oyj",
    "WITH.HE": "WithSecure Oyj",
    "YIT.HE": "YIT Oyj",
    "LEADD.HE": "LeadDesk Oyj",
    "MODU.HE": "Modulight Oyj",
    "ALEX.HE": "Alexandria Pankkiiriliike Oyj",

    # Stockholm (OMX Stockholm)
    "LIME.ST": "Lime Technologies AB",
    "MSAB-B.ST": "Micro Systemation AB",
    "BETCO.ST": "Betsson AB",
    "BICO.ST": "BICO Group AB",
    "BIOA-B.ST": "BioArctic AB",
    "RVRC.ST": "RevolutionRace (RVRC) AB",
    "SECARE.ST": "Secits Holding AB",
    "AVT-B.ST": "Avtech Sweden AB",
    "CAG.ST": "CAG Group AB",
    "CHEF.ST": "Cheffelo AB",
    "UPSALE.ST": "Upsales Technology AB",
    "INWI.ST": "Inwido AB",
    "MIPS.ST": "Mips AB",
    "CEVI.ST": "Cevian Capital AB",
    "SF.ST": "Stillfront Group AB",
    "ENRO.ST": "Eniro Group AB",
    "DEDI.ST": "Dedicare AB",
    "IBT-B.ST": "Infant Bacterial Therapeutics AB",
    "ATIC.ST": "Actic Group AB",
    "MAGI.ST": "Mag Interactive AB",
    "DOXA.ST": "Doxa AB",
    "TRIAN-B.ST": "Trianon AB",
    "CTM.ST": "Catena Media plc",
    "AJA-B.ST": "Anoto Group AB",
    "NTEK-B.ST": "Nordic Technology Group AB",
    "GARO.ST": "Garo AB",
    "SLEEP.ST": "Sleep Cycle AB",
    "4C.ST": "4C Group AB",
    "AGROUP.ST": "A-Group Holding AB",
    "CARE.ST": "Careium AB",
    "DORMY.ST": "Dormy Golf AB",
    "EMBELL.ST": "Embellence Group AB",
    "EQL.ST": "EQL Pharma AB",
    "FAG.ST": "Fagerhult Group AB",
    "FNOVA-B.ST": "Fastpartner AB",
    "GENI.ST": "Genivis AB",
    "ISOFOL.ST": "Isofol Medical AB",
    "KLAR.ST": "KlaraBo Sverige AB",
    "KOBR-B.ST": "Kobran AB",
    "KRONA.ST": "Kronaby AB",
    "NEPA.ST": "Nepa AB",
    "NXTCL.ST": "NextCell Pharma AB",
    "NIVI-B.ST": "Nivika Fastigheter AB",
    "NREST.ST": "Nordic Restructuring AB",
    "OPTER.ST": "Opter AB",
    "ORT-B.ST": "Ortivus AB",
    "PTRK.ST": "Patrik AB",
    "QSYS.ST": "Q-System AB",
    "SOF-B.ST": "Softronic AB",
    "TEQ.ST": "Teqcycle AB",
    "BEAMMW-B.ST": "Beamreach Solar AB",

    # US / Global Equities & ETFs
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft Corporation",
    "NVDA": "NVIDIA Corporation",
    "GOOGL": "Alphabet Inc. (Google)",
    "GOOG": "Alphabet Inc. (Google)",
    "AMZN": "Amazon.com Inc.",
    "META": "Meta Platforms Inc.",
    "TSLA": "Tesla Inc.",
    "QDVE.DE": "iShares S&P 500 Info Tech ETF",
    "EXUS.DE": "Xtrackers MSCI World ex USA ETF",
    "XESC.DE": "Xtrackers Euro Stoxx 50 ETF",
    "DFNS.L": "VanEck Defense ETF",
    "ISPY.L": "L&G Cyber Security ETF",
    "NN_SUOMI": "Nordnet Indeksirahasto Suomi",
    "NN_SVERIGE": "Nordnet Indeksfond Sverige",
    "NN_NORGE": "Nordnet Indeksfond Norge",
    "NN_TANSKA": "Nordnet Indeksfond Danmark",
}

# In-memory cache loaded from disk and CSVs
_MEMORY_CACHE: Optional[Dict[str, str]] = None


def _load_names_cache() -> Dict[str, str]:
    """Loads and builds the unified ticker -> name lookup dictionary."""
    global _MEMORY_CACHE
    if _MEMORY_CACHE is not None:
        return _MEMORY_CACHE

    names: Dict[str, str] = dict(STATIC_COMPANY_NAMES)

    # 1. Load from universe CSVs
    for fn in ("nordnet_universe.csv", "nordnet_global_universe.csv"):
        p = DATA_DIR / fn
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        t = (r.get("Ticker_YF") or "").strip().upper()
                        n = (r.get("Name") or "").strip()
                        if t and n and t not in names:
                            names[t] = n
            except Exception as e:
                logger.debug(f"Could not load company names from {fn}: {e}")

    # 2. Load from persistent disk cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
                if isinstance(disk_data, dict):
                    for k, v in disk_data.items():
                        if k and v and k not in names:
                            names[k.upper()] = str(v)
        except Exception as e:
            logger.debug(f"Could not load disk cache {CACHE_FILE}: {e}")

    _MEMORY_CACHE = names
    return _MEMORY_CACHE


def _persist_cache_item(ticker: str, name: str) -> None:
    """Appends a new ticker -> name mapping to data/company_names_cache.json."""
    try:
        current_data = {}
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    current_data = json.load(f)
            except Exception:
                current_data = {}
        current_data[ticker.upper()] = name
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(current_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"Failed to persist company name to {CACHE_FILE}: {e}")


def get_company_name(ticker: str, default: Optional[str] = None) -> str:
    """Returns the official company name for a ticker symbol.

    Examples:
      get_company_name("MEKKO.HE") -> "Marimekko Oyj"
      get_company_name("LIME.ST")  -> "Lime Technologies AB"
      get_company_name("AAPL")     -> "Apple Inc."

    If not found in memory, static list, or CSVs, queries yfinance and caches
    the result. If yfinance fails, returns the default or ticker symbol.
    """
    if not ticker:
        return default or ""

    t_clean = str(ticker).strip().upper()
    cache = _load_names_cache()

    if t_clean in cache:
        return cache[t_clean]

    # Try matching without dot suffix if present
    base_t = t_clean.split(".")[0]
    if base_t in cache and "." not in base_t:
        return cache[base_t]

    # Fallback to yfinance lookup
    try:
        import yfinance as yf
        t_inst = yf.Ticker(t_clean)
        info = getattr(t_inst, "info", None)
        if isinstance(info, dict):
            found_name = info.get("longName") or info.get("shortName")
            if found_name:
                name_clean = str(found_name).strip()
                cache[t_clean] = name_clean
                _persist_cache_item(t_clean, name_clean)
                return name_clean
    except Exception as e:
        logger.debug(f"yfinance company name resolution failed for {t_clean}: {e}")

    # Fallback
    return default if default is not None else t_clean


def format_company_title(ticker: str, company_name: Optional[str] = None) -> str:
    """Formats a title string combining company name and ticker.

    Example:
      format_company_title("MEKKO.HE") -> "Marimekko Oyj (MEKKO.HE)"
    """
    if not ticker:
        return ""
    t_clean = str(ticker).strip().upper()
    c_name = company_name or get_company_name(t_clean)
    if c_name and c_name.upper() != t_clean:
        return f"{c_name} ({t_clean})"
    return t_clean
