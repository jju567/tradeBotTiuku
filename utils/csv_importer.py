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
    "nordnet suomi": "NN_SUOMI",
    "nordnet tanska": "NN_TANSKA",
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
    "viafin service": "VIAFIN.HE",
    "viafin": "VIAFIN.HE",
    "micro systemation": "MSAB-B.ST",
    "msab": "MSAB-B.ST",
    "eq": "EQV1V.HE",
    "enento": "ENENTO.HE",
    "betsson": "BETCO.ST",
    "bico group": "BICO.ST",
    "bioarctic": "BIOA-B.ST",
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


# Keywords expected in Nordnet CSV header rows (Finnish, Swedish, English)
_HEADER_KEYWORDS = ("nimi", "antal", "määrä", "instrument", "name", "quantity", "gak", "keskikurssi")


def read_bytes_lines(content_bytes: bytes) -> List[str]:
    """Tries reading raw CSV bytes using common Nordnet export encodings (UTF-16LE, UTF-16, UTF-8-BOM, UTF-8, CP1252).

    Validates that decoded content looks like a Nordnet portfolio export by
    checking for known header keywords in the first two lines.
    """
    encodings = ["utf-16", "utf-16le", "utf-8-sig", "utf-8", "cp1252"]
    for enc in encodings:
        try:
            content = content_bytes.decode(enc)
            if not content or "\x00" in content:
                continue
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            if not lines:
                continue
            header_text = " ".join(lines[:2]).lower()
            if any(kw in header_text for kw in _HEADER_KEYWORDS):
                logger.info(f"Successfully decoded CSV bytes using encoding '{enc}'")
                return lines
        except Exception:
            continue

    logger.warning("Could not auto-detect encoding for CSV bytes. Falling back to UTF-8 with replacement.")
    content = content_bytes.decode("utf-8", errors="replace")
    return [line.strip() for line in content.splitlines() if line.strip()]


def read_file_lines(filepath: Path) -> List[str]:
    """Tries reading file using common Nordnet export encodings (UTF-16LE, UTF-16, UTF-8-BOM, UTF-8, CP1252).

    Validates that the decoded content looks like a Nordnet portfolio export by
    checking for known header keywords in the first two lines.
    """
    try:
        with open(filepath, "rb") as f:
            raw_bytes = f.read()
        return read_bytes_lines(raw_bytes)
    except Exception as e:
        logger.warning(f"Error reading bytes from {filepath}: {e}. Falling back to line-by-line UTF-8.")
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return [line.strip() for line in f if line.strip()]


class NordnetCSVImporter:
    """Parses Nordnet portfolio export files (UTF-16/UTF-8, tab or semicolon separated) and updates holdings."""

    @staticmethod
    def parse_lines(
        lines: List[str],
        existing_holdings: Dict[str, Any],
        preserve_hodl: bool = True,
    ) -> Tuple[Dict[str, Any], int, List[Dict[str, Any]]]:
        """Core parsing logic for decoded CSV lines.
        
        Args:
            lines: Decoded CSV lines.
            existing_holdings: Current portfolio holdings dict.
            preserve_hodl: If True, positions with hodl=True are preserved even if absent from CSV.
                          If False, only positions present in the CSV are included (full mirror).

        Returns:
            Tuple of (updated_holdings, imported_count, parsed_rows_detail)
        """
        if not lines:
            raise ValueError("CSV file is empty.")

        # Locate the header line (handles exports where metadata is on line 1 and headers on line 2)
        header_idx = 0
        for i, line in enumerate(lines[:5]):
            line_lower = line.lower()
            if any(kw in line_lower for kw in _HEADER_KEYWORDS):
                header_idx = i
                break

        content_lines = lines[header_idx:]
        header = content_lines[0]
        delimiter = "\t" if "\t" in header else (";" if ";" in header else ",")

        reader = csv.DictReader(content_lines, delimiter=delimiter)

        # Start from HODL-locked positions only if preserve_hodl is requested;
        # otherwise, start from empty dict so the imported CSV represents 100% of real holdings.
        if preserve_hodl:
            updated_holdings: Dict[str, Any] = {
                sym: data
                for sym, data in existing_holdings.items()
                if data.get("hodl", False)
            }
            hodl_preserved = set(updated_holdings.keys())
            if hodl_preserved:
                logger.info(f"Preserved {len(hodl_preserved)} HODL-locked position(s) outside CSV scope: {', '.join(hodl_preserved)}")
        else:
            updated_holdings = {}

        imported_count = 0
        parsed_rows_detail: List[Dict[str, Any]] = []

        for row in reader:
            # Normalize field names (strip whitespace from header keys)
            row_keys = {k.strip(): k for k in row.keys() if k}

            # Locate name, quantity and avg-price columns (Finnish / Swedish / English headers)
            name_key = next(
                (row_keys[k] for k in row_keys if "Nimi" in k or "Instrument" in k or "Name" in k), None
            )
            qty_key = next(
                (row_keys[k] for k in row_keys if "Määrä" in k or "Antal" in k or "Quantity" in k), None
            )
            price_key = next(
                (row_keys[k] for k in row_keys if "Keskikurssi" in k or "GAK" in k or "Avg" in k), None
            )

            if not (name_key and qty_key):
                continue

            raw_name = row[name_key].strip()
            qty = parse_finnish_number(row[qty_key])

            if qty <= 0 or not raw_name:
                continue

            avg_price = 0.0
            if price_key and row.get(price_key, "").strip():
                avg_price = parse_finnish_number(row[price_key])
            if avg_price == 0.0:
                logger.warning(
                    f"avg_price is 0.0 for '{raw_name}' — column '{price_key}' missing or empty. "
                    "P&L calculations will be inaccurate until manually corrected."
                )

            symbol = map_name_to_symbol(raw_name, existing_holdings)

            # Carry over user-configured fields from previous state if available
            existing_item = existing_holdings.get(symbol, {})
            target_weight = existing_item.get("target_weight", 0.10)
            hodl_flag = existing_item.get("hodl", False)
            note_val = existing_item.get("note", None)

            holding_data: Dict[str, Any] = {
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
            parsed_rows_detail.append({
                "symbol": symbol,
                "name": raw_name,
                "quantity": holding_data["quantity"],
                "avg_price": holding_data["avg_price"],
                "target_weight": holding_data["target_weight"],
                "is_hodl": hodl_flag,
                "is_new": symbol not in existing_holdings,
            })
            logger.info(f"Imported holding {symbol} ({raw_name}): {qty} pcs @ {avg_price:.2f} EUR")

        # Log positions that were removed because they were absent from the CSV
        removed = set(existing_holdings.keys()) - set(updated_holdings.keys())
        if removed:
            logger.info(f"Removed {len(removed)} position(s) not present in CSV (likely sold): {', '.join(removed)}")

        return updated_holdings, imported_count, parsed_rows_detail

    @staticmethod
    def import_csv(filepath: Path, existing_holdings: Dict[str, Any], preserve_hodl: bool = True) -> Tuple[Dict[str, Any], int]:
        """Parses Nordnet CSV export file and returns a holdings dict that mirrors the CSV exactly."""
        if not filepath.exists():
            raise FileNotFoundError(f"Portfolio CSV file not found: {filepath}")

        lines = read_file_lines(filepath)
        updated_holdings, count, _ = NordnetCSVImporter.parse_lines(lines, existing_holdings, preserve_hodl=preserve_hodl)
        return updated_holdings, count

    @staticmethod
    def import_csv_bytes(content_bytes: bytes, existing_holdings: Dict[str, Any], preserve_hodl: bool = True) -> Tuple[Dict[str, Any], int]:
        """Parses raw Nordnet CSV export bytes directly and returns a holdings dict."""
        lines = read_bytes_lines(content_bytes)
        updated_holdings, count, _ = NordnetCSVImporter.parse_lines(lines, existing_holdings, preserve_hodl=preserve_hodl)
        return updated_holdings, count

    @staticmethod
    def preview_csv_bytes(content_bytes: bytes, existing_holdings: Dict[str, Any], preserve_hodl: bool = True) -> Dict[str, Any]:
        """Previews parsed holdings from raw CSV bytes without modifying state."""
        lines = read_bytes_lines(content_bytes)
        updated_holdings, count, rows = NordnetCSVImporter.parse_lines(lines, existing_holdings, preserve_hodl=preserve_hodl)
        found_symbols = {r["symbol"] for r in rows}
        removed = [s for s in existing_holdings.keys() if s not in updated_holdings]
        hodl_kept = [s for s in updated_holdings.keys() if updated_holdings[s].get("hodl") and s not in found_symbols]

        return {
            "parsed_positions": rows,
            "total_rows": count,
            "removed_symbols": removed,
            "hodl_preserved_symbols": hodl_kept,
            "updated_holdings": updated_holdings,
        }
