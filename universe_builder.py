"""
Micro-Cap Universe Builder (< $300M Market Cap Filter).

Filters raw ticker lists across US, Finland (.HE), and Sweden (.ST)
by converting market capitalizations into uniform USD amounts
and strictly filtering out companies exceeding $300M USD.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
import yfinance as yf

# Configure cross-platform terminal encoding (Windows cp1252 fix)
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("universe_builder")


DEFAULT_RAW_TICKERS = [
    # 🇫🇮 Finnish Equities (OMX Helsinki & First North)
    "KAMUX.HE", "ROBIT.HE", "FARON.HE", "REMEDY.HE", "SSH1V.HE", "FODELIA.HE",
    "ANORA.HE", "HONBS.HE", "SOLTEQ.HE", "EEZY.HE", "ASPO.HE", "PUUILO.HE",
    "MUSTI.HE", "QTCOM.HE", "HARVIA.HE", "KEMIRA.HE", "NOKIA.HE", "TIETO.HE",
    "BIOHIT.HE", "DETEC.HE", "ENERS.HE", "LEADD.HE", "NXTMS.HE", "OPTOM.HE",
    "PUMU.HE", "RAUTE.HE", "TALEN.HE", "VIAFIN.HE", "WULFF.HE", "AALLON.HE",
    "BOREO.HE", "DIGIA.HE", "DOVRE.HE", "ENENTO.HE", "EXEL.HE", "INCAP.HE",
    "KREATE.HE", "MARIME.HE", "NOOHO.HE", "NURM.HE", "PIHLIS.HE", "QPR1V.HE",
    "RELAS.HE", "SCANFL.HE", "SIILI.HE", "SITOWS.HE", "SUOM1V.HE", "TAAL.HE",
    "TAMTR.HE", "TECN1V.HE", "VERK.HE", "VINCIT.HE", "WETTER.HE", "WITH.HE",
    # 🇸🇪 Swedish Equities (Spotlight, First North Stockholm, OMX Stockholm)
    "SENS.ST", "PEXA-B.ST", "LUC.ST", "B3.ST", "EVO.ST", "SBB-B.ST", "SINCH.ST",
    "ACAST.ST", "ANOT.ST", "BETS-B.ST", "CINT.ST", "CTEK.ST", "EO.ST", "GIGS.ST",
    "HEXI.ST", "KDEV.ST", "MAG.ST", "MANG.ST", "NOTE.ST", "OVZON.ST", "PCELL.ST",
    "PRIC-B.ST", "QBNK.ST", "RAY-B.ST", "SEDANA.ST", "STIL.ST", "TOBII.ST", "VICO.ST",
    "ACTIC.ST", "ALIG.ST", "AQ.ST", "BICO.ST", "BIOS.ST", "BMAX.ST", "BONEX.ST",
    "CALL.ST", "CANT-B.ST", "CLAS-B.ST", "DDI.ST", "DORO.ST", "DUNI.ST", "EAST.ST",
    "ELAN-B.ST", "ELOS-B.ST", "ENEA.ST", "GARO.ST", "HANZA.ST", "HPOL-B.ST", "HUM.ST",
    "IAR-B.ST", "IMAGE.ST", "ITAB.ST", "KNOW.ST", "LAMM-B.ST", "LIME.ST",
    "MEKO.ST", "MIDW-A.ST", "MILDEF.ST", "MOB.ST", "MSAB-B.ST", "MYCR.ST",
    "NCAB.ST", "NEDER.ST", "NIL-B.ST", "OEM-B.ST", "ORES.ST", "POOL-B.ST",
    "PROB.ST", "RAIL.ST", "SCST.ST", "SEZI.ST", "SIVE.ST", "THQ.ST",
    "TRAD.ST", "TROAX.ST", "VBG-B.ST", "VOLO.ST", "XANO-B.ST",
    # 🇺🇸 US Equities (Micro-Caps & Control Large-Caps)
    "ATOM", "STEM", "WATT", "MVIS", "VUZI", "OSS", "REKR", "BBAI",
    "AEYE", "AKTS", "AMST", "ARBE", "AUST", "BTM", "CLPS", "CPIX", "CREX",
    "CISO", "DUOT", "ENG", "ELTK", "FORA", "GEVO", "HOLO", "IDAI", "INOD",
    "KTRA", "KOPN", "LWAY", "MARK", "MNDR", "MRAM", "POET", "PRSO", "QUIK",
    "REFR", "RKDA", "SGRP", "SISI", "SLNH", "SMTI", "SOBR", "SPNS", "STRR",
    "VERI", "VJET", "WISA", "WRAP", "ZEV", "AIRS", "AEHR", "CAMP",
    "CEMI", "CODX", "CPHI", "CRNT", "CSBR", "CTRN", "DAVE", "DGLY", "DRIO",
    "EGAN", "EMKR", "EYEN", "FLUX", "GLBS", "GLTO", "GROW", "HEAR", "HSON",
    "HUT", "ICLK", "IDN", "IMBI", "IMXI", "IPDN", "ISDR", "IZEA", "JAKK",
    "KBAL", "KVHI", "LAKE", "LAND", "LEGH", "LPRO", "LPSN", "LTRX", "LUNA",
    "MCHX", "MIGI", "MIND", "MRIN", "MSBI", "MTC", "MYMD",
    "NNBR", "NRC", "NTIC", "NVCR", "NWFL", "NXGL", "OB", "OMER",
    "OPRA", "OPTT", "OSUR", "PAVM", "PCB", "PCYG", "PDEX", "PERI", "PLSE",
    "PNTG", "PPSI", "PRCH", "PSTI", "PSTV", "PWFL", "QLGN", "QNST", "RCAT",
    "RDCM", "RGTI", "RIME", "RMTI", "RNGR", "SGBX", "SHIP", "SINT", "SIOX",
    "SLP", "SOLY", "SOND", "SPCB", "SSNT", "SVMH", "TC", "TCON", "TELL",
    "TGB", "THTX", "TMDI", "TPST", "TRDA", "TRT", "TRVI", "TUEM", "TZOO",
    "UAMY", "UEIC", "UFAB", "ULH", "UNFI", "URG", "USAU", "USEG", "USIO",
    "UTSI", "UXIN", "VAPO", "VBIX", "VERB", "VHI", "VINE", "VISL", "VIVE",
    "VMD", "VOXX", "VRDN", "VWE", "WB", "WEYS", "WIMI", "WKEY", "WNEB",
    "WRN", "WSTG", "WTMA", "XELB", "XENT", "XGN", "XNET", "XPL", "XTLB",
    "YGMZ", "YTRA", "ZOM",
    # Control / Exclusion Verification Tickers (> $300M)
    "APPS", "PLUG", "FSLY", "SOFI", "UPST", "AFRM", "SNAP", "PINS",
    "ROKU", "DOCU", "ESTC", "PATH", "MDB", "NET", "CRWD", "SNOW", "AMD", "NVDA"
]


class MicroCapUniverseBuilder:
    """
    Builds clean micro-cap universe by filtering by USD market cap limit (< $300M)
    using point-in-time / historical FX conversion to eliminate look-ahead bias.
    """

    def __init__(
        self,
        raw_tickers: Optional[List[str]] = None,
        max_market_cap_usd: float = 300_000_000.0,
        min_share_price: float = 0.10,
        min_adv_usd: float = 50_000.0,
        output_csv: str = "data/clean_microcap_universe.csv",
    ):
        self.raw_tickers = [t.strip().upper() for t in (raw_tickers or DEFAULT_RAW_TICKERS)]
        self.max_market_cap_usd = max_market_cap_usd
        self.min_share_price = float(min_share_price)
        self.min_adv_usd = float(min_adv_usd)
        self.output_csv = Path(output_csv)
        self.live_fx_rates: Dict[str, float] = {}
        self._historical_fx_cache: Dict[str, pd.Series] = {}

    def fetch_live_fx_rates(self) -> Dict[str, float]:
        """Fetches current FX rates as fallback."""
        rates = {"USD": 1.0, "EUR": 1.08, "SEK": 0.096}
        try:
            eur_ticker = yf.Ticker("EURUSD=X")
            eur_hist = eur_ticker.history(period="5d")
            if not eur_hist.empty:
                rates["EUR"] = float(eur_hist.iloc[-1]["Close"])

            sek_ticker = yf.Ticker("SEKUSD=X")
            sek_hist = sek_ticker.history(period="5d")
            if not sek_hist.empty:
                rates["SEK"] = float(sek_hist.iloc[-1]["Close"])
        except Exception as e:
            logger.warning(f"Could not update live FX rates: {e}. Using static defaults.")

        self.live_fx_rates = rates
        return rates

    def get_historical_fx_rate(self, currency: str, as_of_date: Optional[str] = None) -> float:
        """
        Retrieves historical FX rate to USD on a specific date to prevent look-ahead bias.
        Fallback to closest available date or live rate.
        """
        curr = currency.upper().strip()
        if curr == "USD":
            return 1.0

        if as_of_date is None:
            if not self.live_fx_rates:
                self.fetch_live_fx_rates()
            return self.live_fx_rates.get(curr, 1.0)

        fx_pair = f"{curr}USD=X"
        if fx_pair not in self._historical_fx_cache:
            try:
                hist = yf.Ticker(fx_pair).history(period="5y")
                if not hist.empty:
                    s = hist["Close"]
                    s.index = pd.to_datetime(s.index).tz_localize(None).strftime("%Y-%m-%d")
                    self._historical_fx_cache[fx_pair] = s
                else:
                    self._historical_fx_cache[fx_pair] = pd.Series(dtype=float)
            except Exception as e:
                logger.warning(f"Failed to fetch historical FX series for {fx_pair}: {e}")
                self._historical_fx_cache[fx_pair] = pd.Series(dtype=float)

        series = self._historical_fx_cache.get(fx_pair)
        if series is not None and not series.empty:
            target_dt = str(as_of_date)[:10]
            # Exact match
            if target_dt in series.index:
                return float(series[target_dt])
            # Closest preceding trading day
            valid_dates = [d for d in series.index if d <= target_dt]
            if valid_dates:
                closest_date = max(valid_dates)
                return float(series[closest_date])

        # Fallback to live rate
        if not self.live_fx_rates:
            self.fetch_live_fx_rates()
        return self.live_fx_rates.get(curr, 1.0)

    def get_company_market_cap_usd(
        self,
        ticker: str,
        as_of_date: Optional[str] = None,
    ) -> Tuple[Optional[float], Optional[float], str, float]:
        """
        Retrieves company market cap and converts to USD using historical FX on as_of_date.
        Returns: (market_cap_usd, market_cap_local, currency, fx_rate_used)
        """
        try:
            t = yf.Ticker(ticker)
            info = getattr(t, "fast_info", None) or getattr(t, "info", {})

            mcap_local = None
            if hasattr(info, "market_cap") and info.market_cap:
                mcap_local = float(info.market_cap)
            elif isinstance(info, dict) and info.get("marketCap"):
                mcap_local = float(info["marketCap"])

            # Currency detection
            currency = "USD"
            if ticker.endswith(".HE"):
                currency = "EUR"
            elif ticker.endswith(".ST"):
                currency = "SEK"
            elif isinstance(info, dict) and info.get("currency"):
                currency = str(info["currency"]).upper()
            elif hasattr(info, "currency") and info.currency:
                currency = str(info.currency).upper()

            if mcap_local is None or mcap_local <= 0:
                hist = t.history(period="5d")
                shares = getattr(info, "shares", None) or (info.get("sharesOutstanding") if isinstance(info, dict) else None)
                if not hist.empty and shares:
                    mcap_local = float(hist.iloc[-1]["Close"]) * float(shares)

            if mcap_local is None or mcap_local <= 0:
                return None, None, currency, 1.0

            rate = self.get_historical_fx_rate(currency, as_of_date=as_of_date)
            mcap_usd = mcap_local * rate
            return mcap_usd, mcap_local, currency, rate

        except Exception as e:
            logger.warning(f"Failed to fetch market cap for {ticker}: {e}")
            return None, None, "USD", 1.0

    def get_liquidity_and_price(
        self,
        ticker: str,
        fx_rate: float,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Retrieves current share price, 20-day ADV in local currency, and 20-day ADV in USD.
        Returns: (current_price, adv_20d_local, adv_20d_usd)
        """
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="3mo")
            if hist.empty:
                return None, None, None

            valid_closes = hist["Close"].dropna()
            if valid_closes.empty:
                return None, None, None

            current_price = float(valid_closes.iloc[-1])

            # 20-day ADV in monetary value (Price * Volume)
            dollar_vol_series = (hist["Close"] * hist["Volume"]).dropna()
            if len(dollar_vol_series) == 0:
                adv_20d_local = 0.0
            else:
                adv_20d_local = float(dollar_vol_series.tail(20).mean())

            adv_20d_usd = adv_20d_local * fx_rate
            return current_price, adv_20d_local, adv_20d_usd

        except Exception as e:
            logger.warning(f"Failed to fetch liquidity data for {ticker}: {e}")
            return None, None, None

    def build_universe(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Filters raw ticker list and returns (clean_microcaps_df, excluded_df).
        Applies:
          1. Market Cap <= max_market_cap_usd (< $300M USD)
          2. Share Price >= min_share_price (>= 0.10 in local currency)
          3. 20-day ADV >= min_adv_usd (>= $50,000 USD normalized value)
        """
        self.fetch_live_fx_rates()
        logger.info(
            f"Filtering {len(self.raw_tickers)} tickers: "
            f"Max Cap: ${self.max_market_cap_usd:,.0f} USD | Min Price: {self.min_share_price:.2f} | "
            f"Min ADV: ${self.min_adv_usd:,.0f} USD..."
        )

        clean_records: List[Dict[str, Any]] = []
        excluded_records: List[Dict[str, Any]] = []

        for idx, ticker in enumerate(self.raw_tickers, 1):
            mcap_usd, mcap_local, currency, fx_rate = self.get_company_market_cap_usd(ticker)

            if mcap_usd is None:
                logger.warning(f"[{idx}/{len(self.raw_tickers)}] ⚠️ {ticker}: No market cap data available. Excluding.")
                excluded_records.append({
                    "ticker": ticker,
                    "market_cap_usd": None,
                    "market_cap_local": None,
                    "currency": currency,
                    "exclusion_reason": "NO_MARKET_CAP_DATA"
                })
                continue

            market = "FI" if ticker.endswith(".HE") else ("SE" if ticker.endswith(".ST") else "US")

            # Fetch liquidity and current price
            current_price, adv_20d_local, adv_20d_usd = self.get_liquidity_and_price(ticker, fx_rate)

            record = {
                "ticker": ticker,
                "market": market,
                "market_cap_usd": round(mcap_usd, 2),
                "market_cap_local": round(mcap_local, 2) if mcap_local else None,
                "currency": currency,
                "current_price": round(current_price, 4) if current_price is not None else None,
                "adv_20d_local": round(adv_20d_local, 2) if adv_20d_local is not None else None,
                "adv_20d_usd": round(adv_20d_usd, 2) if adv_20d_usd is not None else None,
            }

            # Filter 1: Market Cap limit
            if mcap_usd > self.max_market_cap_usd:
                logger.info(f"[{idx}/{len(self.raw_tickers)}] ❌ {ticker}: ${mcap_usd/1e6:.1f}M USD -> EXCLUDED (> $300M)")
                record["exclusion_reason"] = f"EXCEEDS_300M (${mcap_usd/1e6:.1f}M)"
                excluded_records.append(record)
                continue

            # Filter 2: Price data availability
            if current_price is None or adv_20d_usd is None:
                logger.warning(f"[{idx}/{len(self.raw_tickers)}] ❌ {ticker}: No price/volume history -> EXCLUDED")
                record["exclusion_reason"] = "NO_PRICE_VOLUME_DATA"
                excluded_records.append(record)
                continue

            # Filter 3: Penny Stock filter (Price >= 0.10 in local currency)
            if current_price < self.min_share_price:
                logger.info(f"[{idx}/{len(self.raw_tickers)}] ❌ {ticker}: Price {current_price:.4f} {currency} < {self.min_share_price:.2f} -> EXCLUDED (Penny Stock)")
                record["exclusion_reason"] = f"PENNY_STOCK_PRICE ({current_price:.4f} {currency} < {self.min_share_price:.2f})"
                excluded_records.append(record)
                continue

            # Filter 4: Minimum 20-day ADV (Normalized monetary value >= $50,000 USD)
            if adv_20d_usd < self.min_adv_usd:
                logger.info(f"[{idx}/{len(self.raw_tickers)}] ❌ {ticker}: 20d ADV ${adv_20d_usd:,.0f} USD < ${self.min_adv_usd:,.0f} -> EXCLUDED (Low Liquidity)")
                record["exclusion_reason"] = f"LOW_LIQUIDITY_ADV (${adv_20d_usd:,.0f} < ${self.min_adv_usd:,.0f} USD)"
                excluded_records.append(record)
                continue

            # Passed all strict filters
            logger.info(
                f"[{idx}/{len(self.raw_tickers)}] ✅ {ticker}: ${mcap_usd/1e6:.1f}M USD | "
                f"Px: {current_price:.2f} {currency} | ADV: ${adv_20d_usd:,.0f} USD -> ACCEPTED"
            )
            clean_records.append(record)

        clean_df = pd.DataFrame(clean_records)
        excluded_df = pd.DataFrame(excluded_records)

        # Save clean universe to CSV
        self.output_csv.parent.mkdir(parents=True, exist_ok=True)
        clean_df.to_csv(self.output_csv, index=False)
        logger.info(f"Saved {len(clean_df)} verified liquid micro-cap tickers to {self.output_csv}")

        return clean_df, excluded_df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Micro-Cap Universe Builder: Filter equities strictly by market cap (< $300M USD), price (>= 0.10), and ADV (>= $50k USD)."
    )
    parser.add_argument(
        "--max-cap",
        type=float,
        default=300_000_000.0,
        help="Maximum market capitalization in USD (default: $300,000,000).",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=0.10,
        help="Minimum share price in local currency (default: 0.10).",
    )
    parser.add_argument(
        "--min-adv",
        type=float,
        default=50_000.0,
        help="Minimum 20-day Average Daily Volume in USD (default: $50,000).",
    )
    parser.add_argument(
        "--output",
        default="data/clean_microcap_universe.csv",
        help="Output CSV file path for clean universe.",
    )

    args = parser.parse_args()

    builder = MicroCapUniverseBuilder(
        max_market_cap_usd=args.max_cap,
        min_share_price=args.min_price,
        min_adv_usd=args.min_adv,
        output_csv=args.output,
    )

    clean_df, excluded_df = builder.build_universe()

    print("\n" + "=" * 90)
    print("💎 CLEAN & LIQUID MICRO-CAP UNIVERSE (< $300M USD, Price >= 0.10, ADV >= $50k USD)")
    print("=" * 90)
    if not clean_df.empty:
        clean_display = clean_df.copy()
        clean_display["mcap_usd_millions"] = clean_display["market_cap_usd"].apply(lambda x: f"${x/1e6:.1f}M")
        clean_display["adv_usd_display"] = clean_display["adv_20d_usd"].apply(lambda x: f"${x:,.0f}")
        print(clean_display[["ticker", "market", "currency", "current_price", "adv_usd_display", "mcap_usd_millions"]].to_string(index=False))
    else:
        print("No tickers qualified under the thresholds.")

    print("\n" + "=" * 90)
    print(f"🚫 EXCLUDED TICKERS (TOTAL {len(excluded_df)})")
    print("=" * 90)
    if not excluded_df.empty:
        print(excluded_df[["ticker", "currency", "exclusion_reason"]].head(25).to_string(index=False))


if __name__ == "__main__":
    main()
