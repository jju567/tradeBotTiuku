# tradeBotTiuku — GitHub Copilot Instructions

Tämä tiedosto ohjaa GitHub Copilotin koodiehdotuksia tässä projektissa.
Noudata näitä ohjeita kaikessa uudessa koodissa ja muutoksissa.

---

## 🐱 Projektin konteksti

**tradeBotTiuku** on Python-pohjainen portfolio-neuvonantosovellus, joka:
- Hakee osakemarkkinadatan `yfinance`-kirjastolla
- Laskee tekniset indikaattorit (RSI, SMA, EMA, Bollinger Bands)
- Arvioi osakkeet OpenAI GPT-4o -mallilla ja rule-based -varamoottorilla
- Tuottaa tasapainousehdotuksia (ei toteuta kauppoja automaattisesti)
- Generoi Markdown- ja HTML-raportteja suomeksi

**Kielet:** Python 3.10+, HTML, JavaScript  
**Testausframework:** `pytest`  
**Konfiguraatio:** `.env`-tiedosto + `config.py`

---

## 🧪 Yksikkötestausvaatimukset

### Pakolliset periaatteet

1. **Kaikki uusi julkinen funktio ja metodi TÄYTYY saada yksikkötesti** ennen kuin se katsotaan valmiiksi.
2. **Testit sijaitsevat `tests/`-hakemistossa** projektin juuressa, peilaten lähdekodin rakennetta:
   ```
   tests/
   ├── __init__.py
   ├── test_indicators.py        # utils/indicators.py
   ├── test_csv_importer.py      # utils/csv_importer.py
   ├── test_config.py            # config.py
   ├── test_risk_manager.py      # core/risk_manager.py
   ├── test_rebalancer.py        # core/rebalancer.py
   ├── test_portfolio.py         # core/portfolio.py
   └── test_ai_advisor.py        # core/ai_advisor.py
   ```
3. **Käytä `pytest`-frameworkia** — ei `unittest.TestCase`-pohjaisia luokkia ellei ole pakottava syy.
4. **Testifunktioiden nimet:** `test_<mitä_testataan>_<odotettu_tulos>()`, esim. `test_calculate_rsi_returns_50_for_insufficient_data()`.
5. **Ulkoiset riippuvuudet TÄYTYY mockata** — `yfinance`, `openai`, tiedostojärjestelmä (käytä `tmp_path`-fikstuureja tai `unittest.mock.patch`).
6. **Jokainen testi on itsenäinen** — testit eivät saa jakaa mutable-tilaa tai riippua suoritusjärjestyksestä.

### Testikattavuustavoitteet

| Moduuli | Minimikattavuus |
|---|---|
| `utils/indicators.py` | 95 % |
| `utils/csv_importer.py` | 85 % |
| `core/risk_manager.py` | 90 % |
| `core/rebalancer.py` | 85 % |
| `core/portfolio.py` | 80 % |
| `config.py` | 75 % |

Aja kattavuusraportti: `pytest --cov=. --cov-report=term-missing`

### Testirakenne (malli)

```python
# tests/test_indicators.py
import pytest
from utils.indicators import calculate_rsi, calculate_sma

def test_calculate_rsi_returns_50_for_insufficient_data():
    """RSI should return neutral 50.0 when data is too short."""
    result = calculate_rsi([100.0, 101.0], period=14)
    assert result == 50.0

def test_calculate_rsi_overbought_returns_high_value():
    """RSI should return > 70 for consistently rising prices."""
    prices = [float(i) for i in range(1, 30)]
    result = calculate_rsi(prices, period=14)
    assert result > 70.0
```

### Mockaukset

- Käytä `unittest.mock.patch` tai `pytest-mock`-kirjaston `mocker`-fikstuureja
- Mockaa `yfinance.Ticker` aina — ei oikeita verkkoyhteyksiä testeissä
- Mockaa `openai.OpenAI` aina — ei oikeita API-kutsuja testeissä
- Käytä `tmp_path` (pytest built-in) tiedostojärjestelmäoperaatioihin

```python
# Esimerkki: yfinance mock
def test_market_data_client_handles_empty_history(mocker):
    mock_ticker = mocker.patch("yfinance.Ticker")
    mock_ticker.return_value.history.return_value = pd.DataFrame()
    ...
```

---

## 🐍 Python-parhaat käytännöt

### Tyyppivihjeet (Type Hints)

- **Kaikissa julkisissa funktioissa TÄYTYY olla tyyppivihjeet** sekä parametreille että paluuarvoille.
- Käytä `from typing import Dict, List, Any, Optional, Tuple` tai Python 3.10+ syntaksia (`dict[str, Any]`).
- `None`-paluuarvo merkitään `-> None`.

```python
# Oikein
def calculate_commission(symbol: str, trade_value: float) -> float:
    ...

# Väärin
def calculate_commission(symbol, trade_value):
    ...
```

### Docstringit

- **Kaikilla julkisilla luokilla ja funktioilla TÄYTYY olla docstring.**
- Käytä Google-tyylisiä docstringejä:

```python
def calculate_rsi(prices: list[float], period: int = 14) -> float:
    """Calculates Relative Strength Index (RSI) for a price series.

    Args:
        prices: List of closing prices, most recent last.
        period: RSI lookback period. Defaults to 14.

    Returns:
        RSI value between 0 and 100. Returns 50.0 if data is insufficient.
    """
```

### Virheenkäsittely

- **Älä koskaan käytä paljaita `except:`** — sieppaa aina tietty poikkeustyyppi.
- Kirjaa poikkeukset `logger.error()` tai `logger.warning()` — älä käytä `print()` tuotantokoodissa.
- Palauta järkevä oletusarvo tai heitä poikkeus ylöspäin — älä hiljaa nollaa virheitä.

```python
# Oikein
try:
    result = risky_operation()
except ValueError as e:
    logger.error(f"Value error in operation: {e}")
    return default_value

# Väärin
try:
    result = risky_operation()
except:
    pass
```

### Vakiot ja konfiguraatio

- **Ei kovakoodattuja arvoja** lähdekoodi-tiedostoissa — kaikki parametrit `config.py`:n tai `.env`:n kautta.
- Moduulitason vakiot kirjoitetaan `SCREAMING_SNAKE_CASE`-muodossa.
- Konfiguroidut polut käyttävät `pathlib.Path` — ei `os.path.join()`.

```python
# Oikein
from pathlib import Path
OUTPUT_DIR = Path(__file__).resolve().parent / "reports"

# Väärin
output_dir = "C:/Users/minä/tradeBotTiuku/reports"
```

### Loggaus

- Jokainen moduuli käyttää omaa loggeriaan: `logger = logging.getLogger(__name__)`.
- Älä käytä `print()` loggaukseen — käytä `logger.info/warning/error/debug`.
- Käytä f-string-interpolointia lokiviesteissä.

```python
logger = logging.getLogger(__name__)
logger.info(f"Fetched {len(results)} symbols in {elapsed:.2f}s")
```

### Nimeämiskäytännöt

| Kohde | Tyyli | Esimerkki |
|---|---|---|
| Muuttujat ja funktiot | `snake_case` | `total_equity`, `calculate_rsi` |
| Luokat | `PascalCase` | `PortfolioManager`, `RiskManager` |
| Vakiot | `SCREAMING_SNAKE_CASE` | `MAX_POSITION_WEIGHT` |
| Yksityiset metodit | `_snake_case` | `_rule_based_tiuku_eval` |
| Testitiedostot | `test_<moduuli>.py` | `test_indicators.py` |
| Testifunktiot | `test_<mitä>_<tulos>` | `test_rsi_overbought_returns_high` |

### Importit

- Järjestys: stdlib → third-party → paikalliset (PEP 8 / isort)
- Ei wildcard-importteja (`from module import *`)
- Suhteelliset importit sallitaan vain pakettien sisällä

```python
# Oikein — järjestys: stdlib, third-party, local
import json
import logging
from pathlib import Path
from typing import Dict, List

import pandas as pd
import numpy as np

import config
from utils.indicators import calculate_rsi
```

### Funktioiden koko ja vastuut

- **Yksittäinen funktio tekee yhden asian** (Single Responsibility Principle).
- Maksimipituus: ~50 riviä per funktio — jaa tarvittaessa apufunktioihin.
- Ei yli 4-tasoista sisäkkäistä koodia — käytä early return -tekniikkaa.

```python
# Oikein — early return
def process_holding(holding: dict) -> dict | None:
    if not holding:
        return None
    if holding.get("quantity", 0) <= 0:
        return None
    # varsinainen logiikka...
```

### Muuttumaton data

- Käytä dataclasseja tai TypedDict-rakenteita kompleksisille tietorakenteille.
- Älä mutatoi funktioon syötettyä dict/list-argumenttia — tee kopio ensin.

```python
# Oikein
updated = dict(existing_holdings)
updated[symbol] = new_data
return updated

# Väärin
existing_holdings[symbol] = new_data  # mutoi alkuperäistä
```

---

## 📁 Hakemistorakenne

```
tradeBotTiuku/
├── .github/
│   └── copilot-instructions.md   # <- tämä tiedosto
├── clients/                      # Ulkoiset API-asiakkaat
├── core/                         # Liiketoimintalogiikka
├── reporting/                    # Raporttien generointi
├── scheduler/                    # Ajastus ja orkestrointi
├── utils/                        # Yleiset apukirjastot
├── tests/                        # Yksikkötestit (pytest)
├── data/                         # Ajon aikaiset JSON-datatiedostot
├── logs/                         # Lokitiedostot
├── reports/                      # Generoidut raportit
├── config.py                     # Konfiguraatiokeskus
└── main.py                       # CLI entry point
```

---

## Kielletyt käytännöt

- `print()` tuotantokoodissa — käytä `logger`
- Paljaat `except:` tai `except Exception:` ilman loggausta
- Kovakoodatut polut, IP-osoitteet tai API-avaimet koodissa
- Oikeat verkko- tai API-kutsut testeissä (mockaa aina)
- Testit jotka riippuvat toisistaan tai globaalista tilasta
- Wildcard-importit (`from x import *`)
- Magiset numerot ilman nimettyä vakiota tai parametria

---

## 📚 Dokumentaatiovaatimukset (README yms.)

1. **Uudet ominaisuudet TÄYTYY aina dokumentoida:** Kun lisäät uusia ominaisuuksia, taustaprosesseja, rajapintoja tai CLI-lippuja, päivitä niiden kuvaus ja käyttöohjeet projektin `README.md`-tiedostoon.
2. **Konfiguraatio-muutokset:** Kaikki uudet `.env`-muuttujat ja `config.py`-vakiot täytyy kuvata ja lisätä `.env.example`-tiedostoon sekä `README.md`-tiedoston konfiguraatio-osioon.
3. **Koodikommentit ja Docstringit:** Pidä yllä ajantasaiset Google-tyyliset docstringit luokille ja funktioille.

