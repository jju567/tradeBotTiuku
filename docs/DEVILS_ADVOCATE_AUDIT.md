# Devil's Advocate -auditointi: Kaupankäyntiparametrit, Vuotokohdat ja Optimistiset Oletukset

**Päivämäärä:** 4.11.2026  
**Kohde:** `tradeBotTiuku` (Moniportfolion kvantitatiivinen osakepoiminta- ja kaupankäyntijärjestelmä, ml. P1–P12 Walk-Forward ja P13 Live Candidate)  
**Rooli:** *Devil's Advocate / Red Team Quantitative Auditor*

---

## Johdanto ja Yhteenveto

Tämä dokumentti on kriittinen *Devil's Advocate* -auditointi, jonka tarkoituksena on haastaa `tradeBotTiuku`-järjestelmän kaupankäyntiparametrit, toimeksiantojen reititysoletukset, kululaskenta, riskienhallinta sekä backtest- ja walk-forward-metodologia. Jokainen löydös perustuu suoraan lähdekoodin (`main_controller.py`, `portfolios_config.yaml`, `config.py`, `screener/*`, `core/*`, `scripts/institutional_backtester.py`) ja tuotetun ajodatan (`data/portfolios/*`) ristiintarkastukseen.

### Kriittisyysluokittelu
- 🔴 **Kriittinen vuotokohta (Critical Leak):** Vääristää suoraan paperisalkun / backtestin tuottoa suhteessa todelliseen tiliin tai altistaa oikean pääoman hallitsemattomalle tappiolle.
- 🟠 **Merkittävä optimistinen oletus (Optimistic Assumption):** Aliarvioi systemaattisesti kaupankäyntikitkaa, kuluja, valuuttakurssivirtoja tai tilastollista epävarmuutta.
- 🟡 **Rakenteellinen ristiriita / Kuollut koodi (Structural Bug / Dead Rule):** Konfiguraatiossa tai dokumentaatiossa määritelty suojamekanismi ei joko toimi koodissa tai kumoaa toisen säännön.

---

## 1. Toimeksiantojen Toteutus, Slippage ja Reititys (Execution Leaks)

### 1.1 🔴 Paperisalkku toteuttaa kaupat päivän $T$ päätöskurssiin (`cand_price`), mutta oikea kaupankäynti tapahtuu päivän $T+1$ avauksessa
- **Sijainti koodissa:** `main_controller.py` rivit 2344–2366 (P13 osto), 2885–2898 (P12 osto), 3065–3077 (P1–P10 osto) sekä `execute_phase1_exits` rivit 1496 ja 1762 (myynnit).
- **Mekanismi:** EOD-ajossa (klo 23:05 EET pörssien sulkeuduttua) salkkumoottori vähentää välittömästi käteisen ja kirjaa osakkeet salkkuun päivän $T$ päätöskurssilla (`Buy Price = cand_price`, `Sell Price = current_price`). Samaan aikaan `P13_Live_Candidate` lähettää sähköpostitse manuaaliset toimeksianto-ohjeet seuraavan aamun ($T+1$) avaukseen tiukalla `+0.2 %` / `-0.2 %` rajahintapuskurilla (`limit_buffer_pct: 0.002`).
- **Vuotokohta (Adverse Selection & Ghost Fills):**
  - Vaikka `audit_p13_next_day_executions` (`main_controller.py:3167–3345`) tarkistaa jälkikäteen päivän $T+1$ OHLCV-datasta, olisiko rajahinta toteutunut (`FILLED_AT_OPEN`, `FILLED_INTRADAY`, `UNFILLED_GAP_UP`, `UNFILLED_GAP_DOWN`), **tämä auditointi kirjoittaa vain lokia (`p13_execution_audit.csv`), eikä koskaan peru tai korjaa `portfolio_P13_Live_Candidate_state.json`-tilatiedoston kauppoja!**
  - **Seuraus ostoissa:** Kun yhtiö julkaisee positiivisen tulos- tai uutiskatalyytin ja avaa seuraavana aamuna $+3\,\%$ gap-upilla (`Open > Prev Close * 1.002`), oikean sijoittajan `LIMIT` (+0.2 %) jää toteutumatta (`UNFILLED_GAP_UP`), mutta P13-paperisalkku omistaa osakkeen eilisen päätöskurssilla ja kirjaa täyden kurssinousun.
  - **Seuraus myynneissä:** Kun osake romahtaa ja laukaisee stop-lossin tai uutishylkäyksen, ja avaa seuraavana aamuna $-5\,\%$ gap-downilla, paperisalkku on jo kirjannut ulosmenon eilisen päätöskurssiin, kun taas oikean sijoittajan myyntilimiitti (`Prev Close - 0.2 %`) jää roikkumaan toteutumattomana putoavaan markkinaan.

### 1.2 🔴 Vaarallinen myyntirajahinta (`Prev Close - 0.2 %`) Stop-Loss- ja hätämyynneissä
- **Sijainti koodissa:** `portfolios_config.yaml` (rivit 4–6, 183–184) ja `email_generator.py` (rivit 185, 231).
- **Mekanismi:** Kaikille P13-myynneille – mukaan lukien `FULL_EXIT` (Stop-Loss, `QUARTERLY_FUNDAMENTAL_BREAKDOWN`, `LLM_NEWS_REJECT`) – generoidaan toimeksianto:
  - `Order Type: LIMIT`
  - `Target Limit Price: Prev Close * (1 - 0.002)` eli vain **-0,2 %** alle edellisen päivän päätöskurssin.
  - `Time-in-Force: FOK_15MIN` (tai `DAY`).
- **Vuotokohta:** Epälikvideissä micro-cap-osakkeissa (ADV 20 000–50 000 €), joissa jo normaali bid-ask-spread on 1,5 %–6,0 %, **-0,2 % rajahinta edellisen päivän päätöskurssista alaspäin ei yllä edes normaaliin ostolaitaan (Bid)**, saati hätätilanteessa, jossa stop-loss tai negatiivinen uutinen on lauennut. Todellisuudessa tiukka myyntilimiitti estää stop-lossin toteutumisen juuri silloin, kun positiosta on pakko päästä eroon.

### 1.3 🔴 P12 "Smart Routing" -rajahintatarkistus on matemaattinen tautologia (Kuollut suojakoodi)
- **Sijainti koodissa:** `main_controller.py` rivit 2843–2854 (`P12_Momentum_Breakout` ostosilmukka).
- **Mekanismi:** Koodi yrittää suojata P12-ostoja ylihinnalta:
  ```python
  ref_px = cand.get("vwap") or cand.get("last_candle_close") or cand_price
  target_limit = round(ref_px * (1.0 + limit_buf), 4)
  if cand_price > target_limit:
      logging.info(f"[{p_id}] ⏭️ SKIPPED {ticker}: Price {cand_price:.2f} exceeded Smart Limit {target_limit:.2f}")
      continue
  fill_price = min(cand_price, target_limit)
  ```
- **Vuotokohta:** Kun `momentum_cache` rakennetaan (`main_controller.py:1028–1034`), sinne tallennetaan vain avaimet `ticker`, `price`, `vol_surge_pct`, `ret_5d`, `atr_pct` ja `meta`. Avaimia `vwap` tai `last_candle_close` **ei koskaan aseteta**. Näin ollen `ref_px` on aina täsmälleen `cand_price`, ja `target_limit = cand_price * 1.002`. Ehto `if cand_price > cand_price * 1.002` on matemaattisesti **aina epätosi (False)** positiivisilla hinnoilla, ja `fill_price` on aina `cand_price`. Koko P12 Smart Limit -suojaus on näennäiskoodia.

### 1.4 🔴 EOD-tason Stop-Loss-vuoto (Ei päivänsisäistä stop-toteutusta live-daemonissa)
- **Sijainti koodissa:** `main_controller.py` (`execute_phase1_exits`, rivit 1650–1760) vs. `scripts/institutional_backtester.py` (rivit 240–244).
- **Mekanismi:** Backtesterissä stop-loss tarkistetaan päivän alinta kurssia (`Low`) vasten ja toteutetaan tasoon `min(Open, trailing_stop)`. Live/walk-forward-kontrollerissa stop-loss tarkistetaan kuitenkin vain kerran vuorokaudessa EOD-ajossa päivän päätöskurssia (`current_price`) vasten, eikä välittäjälle jätetä lepääviä stop-toimeksiantoja.
- **Empiirinen todiste (`data/portfolios/portfolio_P12_Momentum_Breakout_history.csv`):**
  Vaikka P12-salkulle on määritelty "tiukka 6 % trailing stop" (`trailing_stop_pct: 0.06`), toteutuneet tappiot lokissa ovat:
  - `OBDU-B.ST`: **-76,1 %** (`Peak 1.65 -> Exit 0.40`, tappio `-17 180,06 SEK`)
  - `CPHI`: **-28,0 %** (`Peak 1.18 -> Exit 0.85`)
  - `IPDN`: **-14,8 %** (`Peak 4.61 -> Exit 3.93`)
  - `XBRANE.ST`: **-10,0 %** (`Peak 13.95 -> Exit 12.55`)
  Volatiileissa 3x volyymipiikin micro-capeissa pelkkä EOD-tarkistus tekee 6 % trailing stopista illuusion.

---

## 2. Kaupankäyntikulut, Valuuttakulut (FX) ja Verotus (Cost & Friction Leaks)

### 2.1 🔴 OST:n 0,25 % valuutanvaihtokulu (FX Fee) lasketaan metatietoihin, mutta sitä EI KOSKAAN vähennetä salkun käteisestä tai nettotuotosta
- **Sijainti koodissa:** `main_controller.py` rivit 2338–2342 (P13 osto), rivi 1529 (Partial TP), rivi 1794 (Täysi myynti).
- **Mekanismi:**
  ```python
  total_cost_acc = (actual_gross_buy * fx_to_acc) + fee_acc
  est_fx_fee_eur = round(actual_gross_buy * fx_to_acc * 0.0025, 2)
  state["cash"] = round(cash_balance - total_cost_acc, 2)
  ```
- **Vuotokohta:** `est_fx_fee_eur` (0,25 % suuntaansa eli 0,50 % edestakaisin Nordnetin OST/AOT-valuutanvaihdossa USD/SEK/DKK/GBP-kaupoissa) tallennetaan kyllä position `"Estimated_FX_Fee_EUR"`-kenttään, mutta **`total_cost_acc` ja myynnin `net_return_acc` eivät sisällä sitä lainkaan!** Salkkujen `cash`-saldo ja `Net PnL` -historia yliarvioivat jokaisen ulkomaisen kaupan tulosta 0,50 prosenttiyksiköllä (ja P1–P12 eivät laske edes tätä metatietoa).

### 2.2 🔴 Kolme keskenään ristiriitaista välityspalkkiomallia (Ulkomaiset kaupat alihinnoiteltu)
Järjestelmässä on kolme eri kululaskuria, jotka antavat täysin eri tuloksen:
1. **`config.py` (rivit 81–104):** Määrittelee Nordnetin todellisen hinnaston, jossa ulkomaisten pörssien (US, `.ST`, `.DE`, `.L`) minimipalkkio perustasoilla (Tier 3/4) on **15,00 € / toimeksianto** (30,00 € edestakaisin) ja Helsingin (`.HE`) 7,00–9,00 €.
2. **`main_controller.py` (rivit 271, 310–312):** Kovakoodaa `MIN_BROKER_FEE = 9.00` € ja `VARIABLE_BROKER_FEE_PCT = 0.002` (0,20 %) **kaikille markkinoille**. Jokainen USA:n ja Ruotsin kauppa alihinnoitellaan 6,00 eurolla suuntaansa (12,00 € per kierros) verrattuna `config.py`-tiedoston hinnastoon.
3. **`scripts/institutional_backtester.py` (rivi 103):** Vähentää vain **0,50 % kokonaiskulun edestakaisin** (`slippage_penalty_pct = 0.50`) sisältäen *sekä* välityspalkkiot *että* bid-ask-spreadin/slippagen!

#### Matemaattinen todellisuustarkistus (P13 Satelliittipositio 1 185 € vs. P1–P4 positio 790 €):
| Kulukomponentti (Edestakainen kauppa, US/SE) | Backtester (`0.50%`) | `main_controller.py` (`9 € min, 0% FX`) | Todellisuus (Nordnet 15 € min + 0.50 % FX + Spread) |
| :--- | :---: | :---: | :---: |
| **Välityspalkkiot (1 185 € positio, P13 Satelliitti)** | Sis. 0,50 %:iin | 18,00 € (1,52 %) | 30,00 € (2,53 %) |
| **Valuutanvaihto (0,25 % + 0,25 %)** | 0,00 € (0,00 %) | 0,00 € vähennetty (0,00 %) | 5,93 € (0,50 %) |
| **Spread / Slippage (Micro-cap, sallittu jopa 6,0 %)** | Sis. 0,50 %:iin | 0,00 % (päätöskurssi) | ~1,50 % – 3,00 % |
| **YHTEENSÄ (% positiosta 1 185 €)** | **0,50 % (~5,93 €)** | **1,52 % (18,00 €)** | **4,53 % – 6,03 % (~54 € – 71 €)** |

Backtesterin `0.50 %` kokonaiskitka aliarvioi todellisen micro-cap-kaupankäyntikitkan pienillä salkuilla **9–12-kertaisesti**.

### 2.3 🟠 Partial Take-Profit (+25 % -> myy 50 %) synnyttää "Triplakululoukun" pienissä salkuissa
- **Sijainti koodissa:** `portfolios_config.yaml` (rivit 14–55, P1–P4: `start_cash: 7900`, `slots: 10`, `partial_take_profit: true`) ja `main_controller.py` (rivit 1490–1550).
- **Mekanismi:** P1–P4-salkuissa yhden slotin koko on $7\,900\text{ €} / 10 = 790\text{ €}$. Kun osake nousee $+25\,\%$ (arvo ~987 €), järjestelmä myy puolet (~493 €) ja jättää loput ~493 € juoksemaan.
- **Vuotokohta:** Koska kiinteä minimipalkkio (9 € tai 15 €) laukeaa jokaisesta toimeksiannosta erikseen, 790 euron positio maksaa **kolme minimipalkkiota** (1x osto + 1x osittaismyynti + 1x loppumyynti = 27 € – 45 € pelkkiä palkkioita eli **3,4 % – 5,7 %** alkuperäisestä pääomasta). Pienissä positioissa osittainen voitonkotiutus syö merkittävän osan asymmetrisesta tuotosta.

### 2.4 🟡 AOT / OY -veroprofiilin kovakoodattu alkupääoma ja puuttuva verolaskenta
- **Sijainti koodissa:** `main_controller.py` rivit 1325–1362.
- **Mekanismi:** Jos `account_tax_profile` vaihdetaan arvoon `"AOT"`, rivi 1330 ylikirjoittaa salkkujen P5, P9, P10 ja P11 alkupääomaksi `11 250 €` (olettaen 10 000 € peruspääoman, vaikka `portfolios_config.yaml` määrittelee kaikille salkuille `7 900 €`). Lisäksi AOT- tai OY-tilassa järjestelmä **ei vähennä 30 % / 34 % luovutusvoittoveroa tai 20 % yhteisöveroa** realisoiduista voitoista, jolloin korkoa korolle -efekti (compounding) yliarvioidaan verollisilla tileillä.

---

## 3. Osakepoiminta, Universumi ja Fundamenttilogiikka (Screening & Logic Leaks)

### 3.1 🔴 First-In-Line -järjestysvinouma (CSV-rivijärjestys) salkuissa P1–P10
- **Sijainti koodissa:** `main_controller.py` rivi 2930 (`for ticker, meta in self.market_engine.universe_metadata.items():`).
- **Mekanismi:** Salkut P11 (järjestys: `Conviction Score`), P12 (järjestys: `vol_surge_pct`) ja P13 (järjestys: `candidate_score`) lajittelevat ostokandidaatit paremmuusjärjestykseen ennen kuin rajalliset salkkuslotit ja Staggered Entry -kiintiöt täytetään. **Salkut P1–P10 EIVÄT lajittele kandidaatteja lainkaan!**
- **Vuotokohta:** Silmukka käy `data/clean_microcap_universe.csv`-tiedostoa läpi rivi riviltä ylhäältä alas ja ostaa ensimmäiset kriteerit täyttävät yhtiöt, kunnes päiväkiintiö (`max_new_positions_per_day`) tai slotit ovat täynnä.
  - Tiedostossa `clean_microcap_universe.csv` rivit 2–11 ovat suomalaisia yhtiöitä (`KAMUX.HE`, `SSH1V.HE`, `ASPO.HE`, `DETEC.HE`, `RAUTE.HE`, `VIAFIN.HE`), rivit 12–30 ruotsalaisia (`NOTE.ST`, `HANZA.ST`, `MOB.ST`, `MSAB-B.ST`...) ja vasta rivit 107–340 myöhemmin lisättyjä yhtiöitä.
  - **Empiirinen todiste (`data/portfolios/portfolio_P1_Base_state.json`):** Salkut P1–P4 ovat ostaneet täsmälleen CSV:n alkupään osakkeet (`VIAFIN.HE`, `MOB.ST`, `MSAB-B.ST`, `EGAN`). CSV:n loppupäässä (rivit 200–340) olevat yhtiöt eivät pääse salkkuihin P1–P10 juuri koskaan, kun salkut ovat täynnä.

### 3.2 🔴 Vakava ristiriita Stop-Loss-tasoissa: Backtestin `-50 %` vs. Live-kontrollerin `-8 % / -12 %`
- **Sijainti koodissa:** `screener/portfolio_manager.py` (rivi 42: `CATASTROPHIC_STOP_PCT = 0.50`) ja `docs/BACKTEST_RAPORTTI.md` vs. `main_controller.py` (rivit 345–346 ja 1726–1737).
- **Mekanismi:** Backtest-raportti (`docs/BACKTEST_RAPORTTI.md`) perustuu siihen, että tiukat stop-lossit tuhoavat micro-cap-tuoton, minkä vuoksi käyttöön otettiin 3-kerroksinen malli, jossa Layer 1 Catastrophic Stop on **-50 %** (antaa multibaggereille tilaa hengittää).
- **Vuotokohta:** `main_controller.py`:n `PortfolioConfig`-luokassa (rivit 345–346) on kuitenkin oletusarvot:
  ```python
  us_stop_loss_pct: float = 0.08
  nordic_stop_loss_pct: float = 0.12
  ```
  Ja `execute_phase1_exits` (rivit 1726–1737) ajaa tämän tarkistuksen **kaikille salkuille P1–P11** ennen kuin `evaluate_position` (-50 %) edes ehtii vaikuttaa!
  - Koska yhdenkään salkun P1–P11 YAML-konfiguraatio ei ylikirjoita näitä arvoja, **kaikissa salkuissa P1–P11 (mukaan lukien `P3_Diamond_Hands`!) on todellisuudessa tiukka -8 % (US) ja -12 % (Nordic) stop-loss!** Backtestattu `-50 %` salkkulogiikka on live-kontrollerissa kokonaan ohitettu.

### 3.3 🟡 P13 `2.5x ATR` dynaaminen Stop-Loss on dokumentoitu YAML-konfiguraatioon, mutta sitä ei ole toteutettu koodiin
- **Sijainti koodissa:** `portfolios_config.yaml` (rivit 155, 166, 177: `"Stop_Loss: -12.0% (or 2.5x ATR)"`) vs. `main_controller.py` (rivit 1666 ja 1702).
- **Vuotokohta:** Koodi tarkistaa pelkästään kiinteän prosenttirajan (`if loss_from_buy >= 0.15:` ja `if loss_from_buy >= 0.12:`). ATR-kerrointa (`2.5x ATR`) ei lasketa eikä verrata missään kohtaa `execute_phase1_exits`-funktiota.

### 3.4 🔴 P13 Satelliitti 2 (`Nordic_MicroCap_Catalyst`) hylkää kaikki kassavirran polttajat, vaikka sen on tarkoitus ostaa `WATCH_TURNAROUND`-käänneyhtiöitä
- **Sijainti koodissa:** `README.md` (rivi 246) ja `screener/tiered_filters.py` (rivit 23–24, 418–453) sekä `main_controller.py` (rivit 2096–2106).
- **Mekanismi:** Strategiakuvauksen mukaan Satelliitti 2 ostaa joko laadukkaita (Profile A/B) micro-capeja TAI `WATCH_TURNAROUND`-käänneyhtiöitä, joilla on vahvistettu uutiskatalyytti ja `cash_runway >= 12 kk`.
- **Vuotokohta (Looginen umpikuja):**
  1. Sekä `tiered_filters.py` (rivi 421: `# 2b. Mandatory positive cash flow gate`) että `main_controller.py` (rivi 2099: `Cash Burner Guard`) **hylkäävät poikkeuksetta jokaisen yhtiön, jolla sekä OCF $\le 0$ että FCF $\le 0$**.
  2. Toisaalta `screener/financial_metrics_engine.py` (rivit 347–361) laskee numeerisen `cash_runway_months`-arvon **vain silloin, kun `ocf_val <= 0 and fcf <= 0`** (positiivisella kassavirralla runway on aina `"Infinite"`).
  3. **Seuraus:** Koska positiivinen kassavirta vaaditaan jo portilla 2b, jokaisella läpi pääsevällä yhtiöllä runway on `"Infinite"`, ja rivi 442 (`runway >= 12.0` tarkistus `WATCH_TURNAROUND`-yhtiöille) on **100 % kuollutta koodia**. Yhtäkään aitoa TTM-kassavirtanegatiivista käänneyhtiötä ei voi päästä seulan läpi.

### 3.5 🔴 P13 CSV-tuonti (`_sync_to_p13_state`) ohittaa fundamenttiseulan ja aiheuttaa välittömiä pakkomyyntejä
- **Sijainti koodissa:** `core/portfolio.py` rivit 286–295 (`_sync_to_p13_state`).
- **Mekanismi:** Kun käyttäjä tuo salkun Nordnetin CSV-tiedostosta, `_sync_to_p13_state` päättelee alastrategian sokeasti pörssipäätteestä:
  - `.HE` $\rightarrow$ `core_nordic_midcap_quality`
  - `.ST` $\rightarrow$ `satellite_nordic_microcap_catalyst`
  - Muut $\rightarrow$ `satellite_us_microcap_growth`
- **Vuotokohta & Empiirinen todiste (`data/portfolios/portfolio_P13_Live_Candidate_history.csv`):**
  - Jos käyttäjä tuo suomalaisen micro-cap-osakkeen (`.HE`, esim. `SSH1V.HE` tai `OPTOMED.HE`), se sijoitetaan automaattisesti `core_nordic_midcap_quality`-koriin. Seuraavassa EOD-ajossa (`main_controller.py:1671–1691`) Core-korin armoton sääntö (`ROE < 8.0 %` tai `EBIT Margin < 5.0 %`) laukaisee välittömän pakkomyynnin (`QUARTERLY_FUNDAMENTAL_BREAKDOWN`)!
  - Tämä tapahtui jo P13-historiassa 20.10.2026, jolloin `SEDANA.ST` (ROE -4,0 %, EBIT -20,5 %) ja `MOB.ST` (ROE 7,6 % < 8,0 %) pakkomyytiin tappiolla heti salkkuun tuonnin jälkeen, koska ne oli luokiteltu `core_nordic_midcap_quality`-strategiaan.

### 3.6 🔴 Fundamenttimoottorin (`financial_metrics_engine.py`) aikajänne- ja skaalavirheet
1. **4 kvartaalin OCF miinus 1 kvartaalin CapEx (Rivit 314–319 & 463–464):**
   - Operatiivinen kassavirta (`ocf_val`) lasketaan 4 kvartaalin TTM-summana (`ttm_quarterly`), mutta investoinnit (`capex`) haetaan joko vuositilinpäätöksestä (`cf_a`) tai **yhden ainoan kvartaalin sarakkeesta (`cf_q.iloc[0]`)** ilman 4 kvartaalin summausta! Kun 12 kuukauden OCF:stä vähennetään vain 3 kuukauden CapEx, vapaa kassavirta (`FCF`) yliarvioidaan merkittävästi.
2. **Vuositilinpäätös ohittaa tuoreimmat kvartaaliluvut liikevaihdon kasvussa ja myyntikatteessa (Rivit 374–413):**
   - `get_hard_financials()` hakee `rev_curr`, `rev_prev` ja `gp_val` **ensin vuositilinpäätöksestä (`inc_a`)** ja käyttää kvartaaliraporttia (`inc_q`) vain, jos vuositilinpäätös puuttuu kokonaan! Tämä tarkoittaa, että Q1-, Q2- ja Q3-raporttien aikana järjestelmä katsoo edellisen vuoden vanhentunutta liikevaihdon kasvua ja bruttokatetta.
3. **`zero_debt_inferred` -optimismi (Rivit 260–273):**
   - Jos `yfinance` ei palauta taseen velkarivejä tunnistettavalla otsikolla (yleinen puute pohjoismaisissa IFRS-taseissa Yahoo Financessa), mutta palauttaa `Total Assets` ja `Cash`, koodi olettaa `debt = 0.0` (`zero_debt_inferred`). Tällöin velkainenkin yhtiö saa `net_cash = True` -statuksen ja pääsee Profile B -laatuputkesta läpi.

---

## 4. Riskienhallinnan ja Markkinaregiimin Katvealueet

### 4.1 🟠 Pohjoismaiset salkut (P5 ja P13:n 85 % Nordic-paino) käyttävät USA:n Russell Microcap (`IWC`) -indeksiä regiimisuodattimena
- **Sijainti koodissa:** `main_controller.py` rivi 916 (`MarketDataEngine.refresh_data`).
- **Mekanismi:** Kaikkien salkkujen – myös `P5_Nordic_Only`-salkun ja 85-prosenttisesti pohjoismaisen `P13_Live_Candidate`-salkun – ostohana (`is_bull_regime`) määräytyy yksinomaan yhdysvaltalaisen `IWC`-ETF:n 200 päivän liukuvan keskiarvon perusteella. Helsingin (`^OMXHPI`) tai Tukholman (`^OMXSPI`) markkinaregiimiä ei tarkisteta.

### 4.2 🟡 Käteispuskuri (`cash_buffer_pct: 0.40`) lasketaan kiinteästä alkupääomasta (`start_cash`), ei nykyisestä salkun arvosta (`total_equity`)
- **Sijainti koodissa:** `main_controller.py` rivi 2453 (`min_required_cash = cfg.start_cash * buffer_pct`).
- **Vuotokohta:** Kun `start_cash = 7900 €` ja `buffer_pct = 0.40`, minimikäteisvaatimus on lukittu kiinteästi **3 160 euroon**.
  - Jos salkku tuplaantuu 15 800 euroon, 3 160 € käteispuskuri on enää **20 %** salkusta (suojaus heikkenee huipulla).
  - Jos salkku laskee 5 000 euroon, 3 160 € pakollinen käteispuskuri lukitsee **63 %** salkusta käteiseksi ja estää ostot pohjalla.

### 4.3 🟡 `portfolio_max_drawdown_alert_pct: -15.0` on kuollut konfiguraatioparametri
- **Sijainti koodissa:** `portfolios_config.yaml` (rivi 186) ja `main_controller.py` (rivi 460).
- **Vuotokohta:** Parametri luetaan `PortfolioConfig`-olioon, mutta **sitä ei kutsuta, tarkisteta eikä käytetä missään kohtaa koodia**. Salkputason hätäjarrutusta (-15 % drawdownissa) ei ole olemassa.

---

## 5. Backtestin, Walk-Forwardin ja NLP-analyysin Tilastolliset Vuodot

### 5.1 🔴 Selviytymisvinouma (Survivorship Bias) ja Yhden Regiimin Harha (`clean_microcap_universe.csv`)
- **Sijainti:** `data/clean_microcap_universe.csv` ja `docs/BACKTEST_RAPORTTI.md` (osio 5.6).
- **Vuotokohta:** Universumi on koottu *tällä hetkellä (2026) pörssissä listatuista* yhtiöistä ja projisoitu taaksepäin ajanjaksolle 02/2025–09/2026. Yhtiöt, jotka menivät konkurssiin, poistettiin pörssistä tai ostettiin pois pilkkahintaan vuosien 2025–2026 aikana, puuttuvat otoksesta. Lisäksi `yfinance` tarjoaa tase- ja tuloslaskelmadatan vain viimeiseltä 4–5 kvartaalilta, minkä vuoksi 71–85 % alkuvuoden 2025 kaupoista (`docs/BACKTEST_RAPORTTI.md:107–111`) tehtiin puutteellisella tasedatalla (`NetCash_Missing`).

### 5.2 🟠 Äärimmäinen oikean hännän riippuvuus (Right-Tail Skew) ja Matala DSR (< 18 %)
- **Sijainti:** `docs/BACKTEST_RAPORTTI.md` (rivit 41–55, 65–74).
- **Vuotokohta:** Kaikissa tutkituissa strategioissa ($N=240$ kauppaa) **mediaanikaupan tuotto on negatiivinen (-3,05 % ... -5,88 %)** ja voittoprosentti alhainen (**36,7 % – 42,1 %**). Positiivinen keskiarvotuotto (+3,57 % ... +4,12 %) syntyy lähes kokonaan 4–5 yksittäisestä multibagger-osakkeesta (`ATOM` +246 %, `TRT` +149 %, `WATT` +129 %, `OSS` +127 %, `QUIK` +102 %).
  - Kun salkussa on vain 5 slottia (P9–P13) ja käytössä on `Staggered Entry Guard` (max 1–2 ostoa päivässä), todennäköisyys sille, että salkku on täynnä muita osakkeita juuri sinä päivänä, kun harvinainen häntävoittaja antaa signaalin, on huomattava.
  - Lisäksi Deflated Sharpe Ratio (DSR) on korkeimmillaankin vain **17,7 %** (kaukana tilastollisesti merkitsevästä 95 % kynnyksestä).

### 5.3 🟠 LLM/NLP-hallusinaatioiden aiheuttamat virheelliset pakkomyynnit (Empiirinen havainto)
- **Sijainti:** `data/portfolios/portfolio_P12_Momentum_Breakout_history.csv` (rivi 11: `HUMBLE.ST`, 19.10.2026) ja `main_controller.py` (rivit 848–860, 1157–1168).
- **Empiirinen todiste:** Ruotsalainen `HUMBLE.ST` pakkomyytiin salkusta, koska Gemini LLM luki monen tickerin RSS-uutisotsikon ja hallusinoi analysoivansa yhdysvaltalaista SPAC-yhtiötä `SPCX`:
  > *"NLP_REJECT (Fatal NLP Risk triggered: The provided information for SPCX is a news headline snippet... Therefore, SPCX is rejected as an empty shell)"*
- Vaikka `main_controller.py`-tiedostoon (rivit 851–858) on myöhemmin lisätty kovakoodattu merkkijonosuodatin (`"is a news headline"`, `"treated as an empty shell"`), suodatin tunnistaa vain nuo täsmälliset englanninkieliset lauseet. Mikä tahansa uudella sanamuodolla tuotettu LLM-väärinkäsitys monen yhtiön uutisotsikosta laukaisee edelleen välittömän `LLM_NEWS_REJECT`-pakkomyynnin.

---

## 6. Yhteenvetotaulukko ja Korjaussuositukset (Action Plan)

| # | Löydös | Vakavuus | Vaikutus | Suositeltu korjaus (`C:\` kehitysympäristössä) |
| :- | :--- | :---: | :--- | :--- |
| **1** | Paperisalkku kirjaa kaupat päivän $T$ päätöskurssiin eikä huomioi $T+1$ avausgappeja | 🔴 Kriittinen | Paperisalkku saa "haamutäytöt" gap-up-voittajista ja välttää gap-down-myyntien hylkäykset | Siirrä P13-paperisalkun kauppojen toteutus odottavaan tilaan (`PENDING_ORDERS`) ja toteuta ne vasta seuraavan päivän ajossa todellisella $T+1$ OHLC-datalla. |
| **2** | Stop-Loss-myynneissä käytetään `LIMIT (Prev Close - 0.2%)` | 🔴 Kriittinen | Stop-Loss jää toteutumatta oikeassa markkinaromahduksessa | Muuta `FULL_EXIT` (Stop-Loss / Breakdown) -myynnit käyttämään joko `MARKET`-toimeksiantoa tai leveämpää suojalimiittiä (esim. `-3.0 %` ... `-5.0 %`). |
| **3** | P12 Smart Limit -tarkistus (`cand_price > cand_price * 1.002`) on aina epätosi | 🔴 Kriittinen | P12-rajahintasuojaus ei tee mitään | Lisää aito päivänsisäinen VWAP tai edellisen päivän Close vertailuhinnaksi `momentum_cache`-rakenteeseen. |
| **4** | EOD Stop-Loss vuotaa rajusti päivänsisäisissä romahduksissa (`OBDU-B.ST` -76 %) | 🔴 Kriittinen | "Tiukka 6 % stop" vuotaa kymmeniä prosentteja | Huomioi päivän `Low`/`Open` walk-forward-simuloinnissa ja ohjeista P12/P13-sähköpostissa asettamaan välittäjälle aito lepäävä Stop-Loss. |
| **5** | OST 0,25 % FX-kulu lasketaan lokiin, mutta ei vähennetä käteisestä (`cash`) | 🔴 Kriittinen | Kaikki USD/SEK-kaupat yliarvioivat nettotuottoa +0,50 %-yksikköä | Lisää `est_fx_fee_eur` suoraan `total_cost_acc`-ostokuluun ja vähennä se `net_return_acc`-myyntitulosta. |
| **6** | Välityspalkkiot alihinnoiteltu (`9 €` vs `15 €` ulkomaat, backtest `0.50 %`) | 🔴 Kriittinen | Backtest aliarvioi pienpositioiden kulukitkan 6–10x | Yhtenäistä `main_controller.py` ja `institutional_backtester.py` käyttämään `config.py`:n markkinakohtaista hinnastoa + realistista spread-mallia. |
| **7** | P1–P10 eivät lajittele ostokandidaatteja (CSV-rivijärjestysvinouma) | 🔴 Kriittinen | Salkut ostavat aina CSV:n alkupään osakkeita (`VIAFIN.HE`, `MOB.ST`) | Järjestä P1–P10 kandidaatit ennen ostosilmukkaa (esim. `Conviction Score` tai `FCF Yield / EV_EBIT`). |
| **8** | `us_stop_loss_pct` (-8 %) ja `nordic_stop_loss_pct` (-12 %) kumoavat `-50 %` Layer 1 stopin | 🔴 Kriittinen | P1–P11 (ml. `P3_Diamond_Hands`) stopataan ulos pienestäkin heilunnasta | Poista kiinteä `-8 % / -12 %` pakkostoppi salkuilta, joiden on tarkoitus noudattaa Tri-Layer (-50 %) tai Diamond Hands -logiikkaa. |
| **9** | P13 Satelliitti 2 vaatii positiivisen kassavirran, mikä estää `WATCH_TURNAROUND`-ostot | 🔴 Kriittinen | `runway >= 12m` -sääntö on kuollutta koodia; käänneyhtiöt hylätään | Salli `tiered_filters.py`:ssä ja `main_controller.py`:ssä `WATCH_TURNAROUND`-profiilille negatiivinen TTM-kassavirta, kunhan `cash_runway_months >= 12` ja katalyytti on vahvistettu. |
| **10** | `_sync_to_p13_state` luokittelee kaikki `.HE`-osakkeet Core-midcapeiksi | 🔴 Kriittinen | Tuodut `.HE` micro-capit pakkomyydään heti, jos ROE < 8 % | Päättele `Sub_Strategy` yhtiön markkina-arvon ja fundamenttiprofiilin perusteella pelkän `.HE`/`.ST`-päätteen sijaan. |
| **11** | `financial_metrics_engine.py`: 4Q OCF - 1Q CapEx sekä vanhentunut vuosikasvu | 🔴 Kriittinen | FCF yliarvioidaan; Q1–Q3 kasvussa käytetään viime vuoden 10-K-lukuja | Laske `capex` 4 kvartaalin TTM-summana (`cf_q.iloc[:, :4].sum()`) ja priorisoi kvartaalikohtainen YoY-kasvu (`inc_q.iloc[0]` vs `inc_q.iloc[4]`). |
| **12** | NLP-hallusinaatio vieraasta tickeristä (`SPCX` vs `HUMBLE.ST`) pakkomyy position | 🟠 Merkittävä | Väärä hälytys realisoi tappion | Varmista NLP-promptissa ja jälkitarkistuksessa, että hylkäysperuste liittyy nimenomaan kohdetickeriin eikä RSS-otsikon sivutickeriin. |
