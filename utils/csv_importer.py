import csv
import logging
from pathlib import Path
from typing import Dict, Any, Tuple, List

logger = logging.getLogger(__name__)

# Known mappings for Nordnet export names -> Tickers
KNOWN_NORDNET_NAMES = {
    "ishares s&p 500 information technology": "QDVE.DE",
    "xtrackers msci world ex usa": "EXUS.DE",
    "faron pharmaceuticals": "FARON.HE",
    "faron": "FARON.HE",
    "xtrackers euro stoxx 50": "XESC.DE",
    "nvidia": "NVDA",
    "vaneck defense": "DFNS.L",
    "l&g cyber security": "ISPY.L",
    "nordnet norge": "NN_NORGE",
    "nordnet sverige": "NN_SVERIGE",
    "nokia": "NOKIA.HE",
    "neste": "NESTE.HE",
    "kone": "KNEBV.HE",
    "sampo": "SAMPO.HE",
    "fortum": "FORTUM.HE",
    "upm": "UPM.HE",
    "elisa": "ELISA.HE",
    "kesko": "KESKOB.HE",
    "valmet": "VALMT.HE",
    "wärtsilä": "WRT1V.HE",
    "orion": "ORNBV.HE",
    "huhtamäki": "HUH1V.HE",
    "kemira": "KEMIRA.HE",
    "tietoevry": "TIETO.HE",
    "stora enso": "STERV.HE",
    "metsä board": "METSA.HE",
    "outokumpu": "OUT1V.HE",
    "qt group": "QTCOM.HE",
    "harvia": "HARVIA.HE",
    "puuilo": "PUUILO.HE",
    "tokmanni": "TOKMAN.HE",
    "gofore": "GOFORE.HE",
    "ponsse": "PON1V.HE",
    "nokian renkaat": "TYRES.HE",
    "kamux": "KAMUX.HE",
    "kempower": "KEMPOWR.HE",
    "anora": "ANORA.HE",
    "verkkokauppa": "VERK.HE",
    "apple": "AAPL",
    "microsoft": "MSFT",
}


def parse_finnish_number(val_str: str) -> float:
    """Converts a Finnish formatted number string (e.g. '32,01' or '2 699,50') to float."""
    if not val_str:
        return 0.0
    clean_str = str(val_str).replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    try:
        return float(clean_str)
    except ValueError:
        return 0.0


def map_name_to_symbol(name: str, existing_holdings: Dict[str, Any]) -> str:
    """Maps a Nordnet export name to a ticker symbol."""
    name_clean = name.strip()
    name_lower = name_clean.lower()

    # 1. Check if name matches any existing holding's name or symbol in tiuku_portfolio.json
    for sym, h in existing_holdings.items():
        stored_name = h.get("name", "").lower()
        if stored_name and (stored_name in name_lower or name_lower in stored_name):
            return sym
        if sym.lower() in name_lower:
            return sym

    # 2. Check KNOWN_NORDNET_NAMES mapping
    for key, sym in KNOWN_NORDNET_NAMES.items():
        if key in name_lower:
            return sym

    # 3. Fallback: sanitize name as symbol if no match found
    fallback = name_clean.upper().replace(" ", "_")
    logger.warning(f"Could not map Nordnet name '{name}' to a known symbol. Using fallback '{fallback}'")
    return fallback


def read_file_lines(filepath: Path) -> List[str]:
    """Tries reading file using common Nordnet export encodings (UTF-16LE, UTF-16, UTF-8-BOM, UTF-8, CP1252)."""
    encodings = ["utf-16", "utf-16le", "utf-8-sig", "utf-8", "cp1252"]
    for enc in encodings:
        try:
            with open(filepath, "r", encoding=enc) as f:
                content = f.read()
                if content and "\x00" not in content:
                    lines = [line.strip() for line in content.splitlines() if line.strip()]
                    if lines and any("nimi" in lines[0].lower() or "antal" in lines[0].lower() or "määrä" in lines[0].lower() for line in lines[:2]):
                        logger.info(f"Successfully decoded CSV using encoding '{enc}'")
                        return lines
        except Exception:
            continue

    # Fallback to UTF-8 with replace
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return [line.strip() for line in f if line.strip()]


class NordnetCSVImporter:
    """Parses Nordnet portfolio export files (UTF-16/UTF-8, tab or semicolon separated) and updates holdings."""

    @staticmethod
    def import_csv(filepath: Path, existing_holdings: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """
        Parses Nordnet CSV export file and returns updated holdings dictionary preserving
        target_weight, hodl, and note fields for existing positions.
        """
        if not filepath.exists():
            raise FileNotFoundError(f"Portfolio CSV file not found: {filepath}")

        lines = read_file_lines(filepath)
        if not lines:
            raise ValueError("CSV file is empty.")

        header = lines[0]
        delimiter = "\t" if "\t" in header else (";" if ";" in header else ",")

        reader = csv.DictReader(lines, delimiter=delimiter)

        updated_holdings = dict(existing_holdings)  # Copy existing holdings
        imported_count = 0

        for row in reader:
            # Normalize field names
            row_keys = {k.strip(): k for k in row.keys() if k}
            
            # Look for stock name, quantity, avg price columns
            name_key = next((row_keys[k] for k in row_keys if "Nimi" in k or "Instrument" in k or "Name" in k), None)
            qty_key = next((row_keys[k] for k in row_keys if "Määrä" in k or "Antal" in k or "Quantity" in k), None)
            price_key = next((row_keys[k] for k in row_keys if "Keskikurssi" in k or "GAK" in k or "Avg" in k), None)

            if not (name_key and qty_key):
                continue

            raw_name = row[name_key].strip()
            qty = parse_finnish_number(row[qty_key])
            avg_price = parse_finnish_number(row[price_key]) if price_key and row[price_key] else 0.0

            if qty <= 0 or not raw_name:
                continue

            symbol = map_name_to_symbol(raw_name, existing_holdings)
            
            # Preserve target_weight, hodl, note if already configured
            existing_item = existing_holdings.get(symbol, {})
            target_weight = existing_item.get("target_weight", 0.10)
            hodl_flag = existing_item.get("hodl", False)
            note_val = existing_item.get("note", None)

            holding_data = {
                "name": raw_name,
                "quantity": int(qty) if qty.is_integer() else round(qty, 4),
                "avg_price": round(avg_price, 4),
                "target_weight": round(target_weight, 4),
            }

            if hodl_flag:
                holding_data["hodl"] = True
            if note_val:
                holding_data["note"] = note_val

            updated_holdings[symbol] = holding_data
            imported_count += 1
            logger.info(f"Imported holding {symbol} ({raw_name}): {qty} pcs @ {avg_price:.2f} EUR")

        return updated_holdings, imported_count
