# 📋 tradeBotTiuku — Muutosyhteenveto (05.11.2026)
## "The Hardened Path" — Datan laadunvarmistus, suojamuurit ja tuotantosynkronointi

Tämä dokumentti kokoaa yhteen kaikki **05.11.2026** tehdyt arkkitehtuuri-, koodi-, suodatin- ja testausmuutokset Devil's Advocate -auditoinnin sekä todellisten pörssiyhtiötapausten (*MIPS, Novotek, RevolutionRace, Puuilo, SSH Communications, Alma Media, Lime, BioGaia*) pohjalta.

---

## 1. Yhteenveto & Pääperiaatteet

Aiempi järjestelmä nojasi useissa kohdin liian optimistisiin oletuksiin, EOD-datan puutteelliseen täsmäytykseen ja jäykkiin sääntökynnyksiin. Tämän päivän aikana järjestelmä vahvistettiin perusteellisesti:

1. **Datan luotettavuus ennen logiikkaa**: Mitään ostopäätöstä tai pisteytystä ei tehdä ennen kuin tilinpäätöserät on poikkileikkauksellisesti auditoitu, päivämäärät todennettu ja API-halkeamat suljettu pois.
2. **Rehelliset markkinakulut ja mekaniikka**: Valuutanvaihdot (FX 0,25 %), kiinteät minimipalkkiot (15 € / 9 €) ja yön yli -avausgapit (`min(Open, stop)`) mallinnetaan rehellisesti.
3. **Pakkolopetuksien poisto kululoukuista**: Satelliitin aikarajat muutettiin tarkistuspisteiksi (`CHECKPOINT_REVIEW_ONLY`), jotta asemia ei realisoida väkisin edestakaiseen kulukuormaan ilman fundamenttimuutosta.
4. **Täysi testikattavuus**: Kaikille havaituille bugeille luotiin tiukat regressiotestit (125/125 hyväksytty).

---

## 2. Rahoitusmoottorin Korjaukset (`screener/financial_metrics_engine.py`)

### A. Päivämäärävarmennettu YoY-Liikevaihtokasvu & NaN-siirtymän Korjaus (MIPS-tapaus)
* **Ongelma**: Jos Yahoo Financen `quarterly_financials` sisälsi `NaN`-arvon (kuten MIPSin Q3 2025), `.dropna()` pudotti rivin hiljaa, jolloin indeksi 0 (Q3 2026) vertautui virheellisesti indeksiin 4 (Q2 2025, 457 päivää aiemmin). Tämä tuotti valheellisen +59,3 % kasvun.
* **Korjaus**: Toteutettiin aito päivämäärävälin tarkistus (`330 <= days <= 400`). Jos vertailukelpoista kvartaalia ei löydy tältä väliltä, vertailu hylätään ja laskenta nojaa todennettuun vuositasoon.
* **TTM vs. Vuosilukujen Täsmäytys**:
  * Jos 4 kvartaalin summa ja vuositilinpäätös päättyvät samaan päivään ja eroavat > 5 %, nostetaan lippu `TTM_ANNUAL_MISMATCH_ERROR`.
  * Jos TTM kasvaa > 25 % yli edellisen vuositilinpäätöksen, nostetaan lippu `TTM_ACCELERATION_OR_MA_DIVERGENCE` yrityskauppojen ja epäorgaanisen hypyn tunnistamiseksi.

### B. Kalenteritietoinen Tuoreussuoja & Yahoo Data Hole -erotus (Puuilo-tapaus)
* **Ongelma**: Aiempi kaavamainen 120 päivän takaraja hylkäsi yhtiöitä, joiden data oli täysin normaalin syklin mukaista (92 pv vuosineljännes + 35–48 pv raportointiviive = luonnostaan ~128 pv juuri ennen seuraavaa raporttia, esim. `RVRC.ST` ja `NTEK-B.ST`). Lisäksi se ei erottanut Yahoo Financen rajapintapuutteita.
* **Korjaus**:
  * **Halkeamavahti (`REJECTED_YAHOO_INCOMPLETE_DATA`)**: Jos tase on tuore mutta tulos- tai rahavirtalaskelma yli 45 päivää vanhempi (`statement_split_days > 45`, esim. Puuilon tase 97 pv vs. tuloslaskelma 278 pv), tapaus kirjataan rajapintavirheeksi, ei yhtiön viivästykseksi.
  * **Syklilogiikka (`REJECTED_STALE_DATA`)**: Kvartaaliraportoijille sallitaan enintään **140 päivää** ja puolivuosiraportoijille **230 päivää**. Jos tulosjulkistus on mennyt yli 3 päivää sitten (`days_to_earnings < -3` ja data > 95 pv) eikä Yahoo ole päivittynyt, asetetaan lippu `STALE_POST_EARNINGS_NOT_UPDATED` ja osto torjutaan.
  * **Statement-kohtainen ikä**: `data_age_days` määräytyy aina **vanhimman käytetyn laskelman mukaan**, ei tuoreimman taseen päivämäärästä.

### C. Suhteellisen Arvostuksen EV/EBIT -korjaukset (SSH1V & Puuilo)
* **`auto_adjust=False`**: Historialliset kurssit haetaan ilman osinkokorjausta, jolloin historiallinen markkina-arvo ja EV/EBIT-mediaani eivät painu keinotekoisen mataliksi.
* **Negatiivisen EBIT:n estäminen**: Jos yhtiön operatiivinen tulos on negatiivinen (`EBIT <= 0`), koodi ei enää koskaan korvaa sitä positiivisella EBITDA-kertoimella. Tulos kirjataan tilaksi `REJECTED_VALUATION_NEGATIVE_EBIT`, mikä torjuu suoraan SSH1V:n kaltaiset tappiolliset yhtiöt.
* **Operating Income vs. EBIT -rivi**: Jos laskelmasta puuttuu `Operating Income`, etsitään ensin `EBIT`-riviä ennen vanhempiin vuosiin putoamista.

### D. Virheiden Käsittely & Reaaliaikaiset Valuutat
* Poistettiin hiljaiset `try/except Exception: pass` -blokit; kaikki poikkeukset lokitetaan (`logger.warning` / `logger.error`).
* Lisättiin `get_live_fx_rates()` reaaliaikaisten EUR/SEK ja EUR/USD -kurssien hakuun (korvaa kiinteät oletusarvot).

---

## 3. Seulontafiltterit & Salkkukohtaiset Portit (`screener/tiered_filters.py`)

1. **Jatkuva 0–100 Pisteytys**:
   * Kaikki pisteytyskaavat sidottiin tiukasti väliin `[0.0, 100.0]`. Estetty keinotekoiset yli 100 pisteen vääristymät.
2. **Pehmeä Kynnyspuskuri (Soft Threshold Buffer)**:
   * Leikkurin sijaan lievästi rajan alle jäävät luvut (ROE 13,5–15,0 % ja EBIT-marginaali 10,5–12,0 %) hyväksytään 0,5x pisteosuudella.
3. **Toimialakohtainen Hajautusvyöhyke**:
   * Jos paras ehdokas toiselta toimialalta on enintään 5,0 pisteen sisällä saman toimialan toisesta osakkeesta, toimialahajautus priorisoidaan automaattisesti.
4. **Rahoitussektorin Sulku Coresta**:
   * Rahoitus- ja pankkisektori suljetaan pois Core MidCap -kannattavuusseulasta, koska pankkien IFRS-taserakenne vääristää perinteisen teollisuuden EBIT- ja nettovelkaluvut.
5. **Pre-Earnings Blackout Gate (0–5 pv)**:
   * 0–5 päivää ennen tulosjulkistusta olevat osakkeet asetetaan automaattiseen ostokieltoon binaarisen tuloskuiluriskin välttämiseksi (`operational_metrics["earnings_gate_blocks"]`).
6. **M&A + Laimennusportti**:
   * Jos yhtiöllä on TTM-liikevaihtohyppy (>25 %) yhdistettynä viimeaikaiseen osakeantiin / laimennukseen (>5 %), automaattinen osto estetään tilassa `REJECTED_MA_DILUTION_MANUAL_REVIEW`.

---

## 4. Kaupankäyntimekaniikka & Salkunhoitaja (`main_controller.py` & `portfolios_config.yaml`)

1. **Satelliitin Uudelleenmäärittely (Trial Protocol)**:
   * USA-mikrokori poistettu käytöstä liiallisten kulujen ja negatiivisen PIT-tuoton vuoksi.
   * Satelliitti määritelty uudelleen: *"Laiminlyödyt, kannattavat pohjoismaiset pienyhtiöt (analyytikot $\le$ 6, päivävaihto $\ge$ 20 000 €, markkina-arvo 15–150 MEUR)"*.
   * Koekausi: 12–24 kuukautta paperilla ja pienellä pääomalla.
2. **Satelliitin Time-Stopin Korjaus (`CHECKPOINT_REVIEW_ONLY`)**:
   * 120 päivän mekaaninen pakkolopetus poistettu kululoukkuna. Korvattu hälyttävällä salkunhoitajan tarkistuspisteellä, jotta hyviä asemia ei myydä 10 % edestakaiseen kulurasitteeseen pelkän kalenterin takia.
3. **Rehellinen Gap-Down Slippage**:
   * Tappionpysäytykset toteutetaan tasoon `min(Open, trailing_stop)`. Avauskuilun aiheuttama todellinen tappio otetaan huomioon täysimääräisenä.
4. **Typical Price -viitehinta**:
   * Päiväkynttilöiden toteutushintana käytetään `typical_price` $\frac{H+L+C}{3}$ -mittaria virheellisen "EOD VWAP" -termin sijaan.
5. **Drawdown-Hätäjarru (`RISK_OFF`)**:
   * Salkun -15 % drawdown huipusta lukitsee uudet ostot ja nostaa käteispuskurin 60 %:iin.

---

## 5. Yhtiökohtaiset Auditoinnit & Ostopäätökset

| Yhtiö | Ticker | Tila | Keskeiset Havainnot ja Päätökset |
| :--- | :--- | :---: | :--- |
| **Alma Media** | `ALMA.HE` | **OSTA (Limiitti 13,60 €)** | Q3 liikevoitto 26,3 MEUR (31,2 % marginaali) oli **100 % operatiivista (oikaistu EBIT = 26,3 MEUR, 0 € kertaeriä)**. FCF-tuotto 7,45 %. Liikevaihto-ohjeistusta nostettu. Limiitti asetettu 13,60 € (14 pv) estämään nousuhuipulta ostaminen. Kulut: 9 € (0,49 % per suunta). |
| **Lime Tech** | `LIME.ST` | **OSTA (Limiitti 264 SEK)** | TTM liikevaihto 799,8 MSEK, jatkuva ARR +11 %, EBITA-marginaali 26,3 %. EV/EBITA 17,9x. Nettovelka 104,6 MSEK (0,49x EBITDA, pääosin IFRS 16 vuokravelkoja). Limiitti 264,00 SEK (14 pv, 76 kpl @ 1 850 €). Kulut: ~19,6 € (1,06 % per suunta). |
| **BioGaia** | `BIOG-B.ST` | **HYLÄTTY (Kallis)** | Kassavirta täsmäytetty sentilleen (EBIT 486,9M + D&A 24,6M - maksetut verot 132,9M - käyttöpääoma 94,6M = OCF 284,5M). Arvostus kuitenkin kallis: EV/EBIT 24x ja FCF-tuotto vain 2,09 %. Karsittu pois. |
| **RevolutionRace**| `RVRC.ST` | **ODOTUSTILASSA (19.11.)** | Poikkeava tilikausi (1.7.–30.6.). Q1 raportointi siirretty päivämäärään **19.11.2026** ICANIWILL AB -yrityskaupan konsolidoinnin vuoksi. Ei osteta ennen raporttia; kolmas slotti pidetään käteisenä. |
| **Novotek** | `NTEK-B.ST` | **EI OSTONA (10.11.)** | **+40,6 % preemiolla suhteessa omaan 5v mediaaniinsa** (EV/EBIT 11,8x vs 8,4x). Tulos pudonnut kurssia (-21,6 %) nopeammin. GE Vernova myi Proficyn pääomasijoittaja TPG:lle (toteutunut 2.3.2026), mikä aiheuttaa jälleenmyyjälle vakavan lisenssi- ja kateriskin. |

---

## 6. Testaus, Git & Tuotanto

1. **Testitulos**:
   * Kaikki **125 testiä** läpäisty virheettömästi:
     * `tests/test_financial_metrics_engine.py`: 21 passed
     * `tests/test_tiered_filters.py`: 24 passed
     * `tests/test_multi_portfolio.py`: 80 passed
2. **Versionhallinta**:
   * Kaikki muutokset commitoitu ja pushattu GitHubiin: [`3b099b7`](https://github.com/jju567/tradeBotTiuku/commit/3b099b7).
   * [`README.md`](file:///c:/Users/Jarmo/Documents/kode/trade/tradeBotTiuku/README.md) päivitetty vastaamaan koodin todellista tilaa.
3. **Tuotantopalvelin (`Z:\tradeBotTiuku`)**:
   * Synkronoitu tuoreimpaan commitiin `3b099b7` turvallisesti ilman paikallisten salkku- tai universumitiedostojen ylikirjoitusta.
   * Taustadaemon pyörii ajastettuna ilta-ajoon klo 23:05.
