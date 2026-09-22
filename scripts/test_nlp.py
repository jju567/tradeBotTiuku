"""
Standalone NLP Test Runner for Helsinki Micro-Cap Stock Screener.

Tests 4 real-world stock exchange disclosure scenarios:
1. Cash Crisis / Liquidity Shortfall (Should be flagged: cash_issue=True)
2. Insider Buying / Manager Transactions (Should be flagged: management_buying=True)
3. Positive Profit Warning / Guidance Upgrade (Should be flagged: positive_guidance=True)
4. Neutral Noise / AGM Notice (No signals: all False)
"""

import os
import sys
import time
import json
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# Add workspace directory to Python path
_current = Path(__file__).resolve().parent
_root = _current.parent if _current.name == "scripts" else _current
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# Import analyze_text from screener package or local module
try:
    from screener.nlp_analyzer import analyze_text
except ImportError:
    try:
        from nlp_analyzer import analyze_text
    except ImportError:
        print("VIRHE: nlp_analyzer.py -tiedostoa ei löytynyt.")
        sys.exit(1)

# 1. Testitapaukset: 4 tyypillistä tilannetta Helsingin pörssin tiedotteista
TEST_CASES = [
    {
        "name": "Kassakriisi / Rahoitusvaje (Pitäisi karsiutua)",
        "expected": {
            "cash_issue": True,
            "insider_buying_personal": False,
            "company_share_buyback": False,
            "positive_guidance": False,
        },
        "text": """Yhtiön hallitus arvioi käyttöpääoman riittävän seuraavan kahden kuukauden ajaksi. 
        Kassatilanteen turvaamiseksi hallitus valmistelee ylimääräiselle yhtiökokoukselle esitystä 
        suunnatusta osakeannista tai velkasaneeraukseen hakeutumisesta. Maksuvalmius on heikentynyt merkittävästi."""
    },
    {
        "name": "Sisäpiiriosto / Johdon henkilökohtainen hankinta (Vahva luottamus)",
        "expected": {
            "cash_issue": False,
            "insider_buying_personal": True,
            "company_share_buyback": False,
            "positive_guidance": False,
        },
        "text": """Johdon liiketoimet: Johtoryhmän jäsen ja toimitusjohtaja on 14.9.2026 hankkinut omaan lukuunsa yhtiön osakkeita 
        Nasdaq Helsingin markkinapaikalla. Transaktion volyymipainotettu keskihinta oli 1,42 euroa ja hankittu 
        kokonaisvolyymi 25 000 kappaletta."""
    },
    {
        "name": "Omien osakkeiden hankinta / Yhtiön osto-ohjelma (Ei henkilökohtainen sisäpiiriosto)",
        "expected": {
            "cash_issue": False,
            "insider_buying_personal": False,
            "company_share_buyback": True,
            "positive_guidance": False,
        },
        "text": """Raute Oyj: OMIEN OSAKKEIDEN HANKINTA viikolla 37, 2026.
        Helsingin Pörssi, Pörssitiedote 15.9.2026.
        Raute Oyj:n hallituksen 18.3.2026 saaman valtuutuksen mukaisesti yhtiö on hankkinut omia osakkeitaan (kaupankäyntitunnus: RAUTE) seuraavasti:
        Päivämäärä: 12.9.2026, Pörssikauppa, Osakemäärä: 1 500 kpl, Keskihinta: 11,20 EUR. Osakkeet hankitaan käytettäväksi osana yhtiön kannustinjärjestelmää."""
    },
    {
        "name": "Positiivinen tulosvaroitus / Ohjeistuksen nosto",
        "expected": {
            "cash_issue": False,
            "insider_buying_personal": False,
            "company_share_buyback": False,
            "positive_guidance": True,
        },
        "text": """Yhtiö nostaa vuoden 2026 taloudellista ohjeistustaan. Alkuvuoden vahvan tilauskannan 
        sekä parantuneen operatiivisen tehokkuuden ansiosta liikevaihdon arvioidaan kasvavan 20-25 % edellisvuodesta 
        (aiemmin 5-10 %) ja oikaistun liikevoiton odotetaan ylittävän 3,5 miljoonaa euroa."""
    },
    {
        "name": "Neutraali / Melu (Ei signaaleja)",
        "expected": {
            "cash_issue": False,
            "insider_buying_personal": False,
            "company_share_buyback": False,
            "positive_guidance": False,
        },
        "text": """Kutsu varsinaiseen yhtiökokoukseen. Osakkeenomistajat kutsutaan koolle tiistaina 28.10.2026. 
        Kokouksessa käsitellään yhtiöjärjestyksen mukaiset asiat, kuten tilinpäätöksen vahvistaminen ja 
        hallituksen jäsenten palkkiot."""
    }
]


def run_test():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("HUOMAUTUS: OPENROUTER_API_KEY puuttuu ympäristömuuttujista.")
        print("Ajetaan testi sääntöpohjaisella varajäsentimellä (deterministic Finnish keyword engine)...")
        print("Määritä OPENROUTER_API_KEY .env-tiedostoon, jos haluat testata suoraa OpenRouter API -kutsua.\n")

    print("=" * 70)
    print("KÄYNNISTETÄÄN NLP -TESTIAJO (Rate limit, Throttling & JSON check)")
    print("=" * 70)

    total_start = time.time()
    passed_count = 0

    for idx, case in enumerate(TEST_CASES, start=1):
        print(f"\n[Testi {idx}/{len(TEST_CASES)}] {case['name']}")
        start_time = time.time()
        
        try:
            # Ajetaan analyysi
            result = analyze_text(case["text"])
            elapsed = time.time() - start_time
            
            print(f"-> Vastausaika: {elapsed:.2f} s")
            print(f"-> Tulos JSON:\n{json.dumps(result, indent=2, ensure_ascii=False)}")
            
            # Tarkistetaan vastaavuus
            matches = (
                result.get("cash_issue") == case["expected"]["cash_issue"] and
                result.get("insider_buying_personal", result.get("management_buying")) == case["expected"]["insider_buying_personal"] and
                result.get("company_share_buyback") == case["expected"]["company_share_buyback"] and
                result.get("positive_guidance") == case["expected"]["positive_guidance"]
            )
            if matches:
                print("-> TULOS: [OK] Odotettu signaali tunnistettu oikein.")
                passed_count += 1
            else:
                print("-> TULOS: [VIRHE] Signaalit eivät vastanneet odotettua!")
                print(f"   Odotettiin: {case['expected']}")
                print(f"   Saatiin:    {result}")
        
        except Exception as e:
            print(f"-> TULOS: [VIRHE] Poikkeus: {e}")

    total_elapsed = time.time() - total_start
    print("\n" + "=" * 70)
    print(f"Kaikki testit suoritettu ajassa {total_elapsed:.2f} s. ({passed_count}/{len(TEST_CASES)} läpäisty)")
    print("=" * 70)

    if passed_count != len(TEST_CASES):
        sys.exit(1)


if __name__ == "__main__":
    run_test()
