# Priorisoitu Toteutussuunnitelma (The Hardened Path): Devil's Advocate -korjaukset

**Päivämäärä:** 5.11.2026  
**Pohjadokumentti:** [docs/DEVILS_ADVOCATE_AUDIT.md](DEVILS_ADVOCATE_AUDIT.md)  
**Kohdeympäristö:** `C:\Users\Jarmo\Documents\kode\trade\tradeBotTiuku` (⚠️ **KRIITTINEN SÄÄNTÖ:** Kaikki kehitys ja testaus tehdään vain `C:\`-asemalla. Älä koskaan ylikirjoita `Z:\`-tuotantoasemaa suoraan.)

---

## Arkkitehtoninen Periaate: "Data Integrity Before Execution"

Järjestelmää ei saa avata uusille riskeille (kuten käänneyhtiöille tai väljemmille stop-losseille) ennen kuin pohjadata, kassavirtojen laskenta ja arvostussuojat ovat 100 % luotettavia. Tämän vuoksi toteutus etenee tiukasti neljässä portissa (**Vaihe 0 $\rightarrow$ Vaihe 1 $\rightarrow$ Vaihe 2 $\rightarrow$ Vaihe 3**).

---

## Vaihe 0: Datan Laatu ja Arvostus (Puuttuvat suojamuurit — TEE NÄMÄ ENSIN)

### `[TASK-00]` Datan Laadunvarmistus (Cross-Sectional Audit & Data Leak Quarantine)
- **Tavoite:** Estetään vuotavan tai kopioituneen datan (esim. kahdella eri osakkeella identtinen `+71,7 %` liikevaihdon kasvu tai EBIT-marginaali välimuistivuodon vuoksi) pääsy pisteytykseen ja kaupankäyntiin, sekä liputetaan `> 100 %` ääriarvot manuaaliseen tarkistukseen.
- **Nykytilan puute:** `audit_candidates_data()` (`screener/financial_metrics_engine.py:1198`) ajetaan tällä hetkellä vain P13-alastrategian sisällä (`main_controller.py:2324`), ja se tuottaa pelkän varoitustekstin (`audit_warning`) estämättä kauppaa.
- **Tarkka toteutusohje:**
  1. **Tiedosto `screener/financial_metrics_engine.py` (`audit_candidates_data`, rivit 1198–1374):**
     - Kun `DATA_DUPLICATE_WARNING` havaitaan (vähintään kahdella eri tickerillä on 4 desimaalin tarkkuudella identtinen ei-nolla `revenue_growth_yoy_pct`, `ebit_margin` tai `roe`):
       - Aseta `cand["data_leak_quarantine"] = True` ja `hard_facts["status"] = "REJECTED_DATA_LEAK"`.
     - Kun `MANUAL_REVIEW_OUTLIER` havaitaan (`revenue_growth_yoy_pct > 100.0 %`, `roe > 100.0 %` tai `abs(ebit_margin) > 100.0 %`):
       - Lisää lippu `"MANUAL_REVIEW_OUTLIER"` listaan `cand["audit_flags"]`.
       - Aseta `cand["requires_manual_review"] = True`.
  2. **Tiedosto `main_controller.py` (`MarketDataEngine.refresh_data` & `execute_phase2_screening`):**
     - Kutsu `audit_candidates_data()` poikkileikkauksellisesti koko `self.hard_facts_cache`-joukolle heti datan latauksen jälkeen **ennen kuin yhtäkään salkkua (P1–P13) skannataan**.
     - Jos yhtiöllä on `status == "REJECTED_DATA_LEAK"` tai `"DATA_DUPLICATE_WARNING" in audit_flags`, **estä automaattinen osto kaikissa salkuissa** (`logger.warning(f"🛑 [DATA LEAK GUARD] Blocking {ticker}: Cross-sectional duplicate metric detected.")`).
- **Validointi (`pytest`):**
  - Syötä kolme osaketta, joista kahdella (`TICK1`, `TICK2`) on täsmälleen sama `revenue_growth_yoy_pct = 71.7123`. Varmista, että molemmat saavat `DATA_DUPLICATE_WARNING`-lipun ja `REJECTED_DATA_LEAK`-statuksen, eivätkä pääse ostolistalle.

---

### `[TASK-00B]` Suhteellinen Arvostussuodatin Core-koriin (EV/EBITDA & EV/EBIT vs. 5v historia)
- **Tavoite:** Pelkkä laatuleima ja momentum eivät riitä kalliiden laatuyhtiöiden suojaksi. Estetään Core-korin (`core_nordic_midcap_quality`) ostot silloin, kun yhtiön nykyinen `EV/EBIT` (tai `EV/EBITDA`) on venynyt merkittävästi yli sen oman 5 vuoden historiallisen mediaanin.
- **Nykytilan puute:** `calculate_relative_ev_ebit()` (`screener/financial_metrics_engine.py:141`) laskee `ev_ebit_premium_pct`-preemion ja asettaa `is_historically_expensive = True` (kun preemio `> +20 %`), mutta `passes_nordic_midcap_compounder()` (`screener/tiered_filters.py:178`) ei käytä sitä suodattimena, ja `main_controller.py:2254` antaa siitä vain `-20` pisteen vähennyksen.
- **Tarkka toteutusohje:**
  1. **Tiedosto `screener/financial_metrics_engine.py` (`calculate_relative_ev_ebit`, rivit 141–275):**
     - Varmista, että funktio tukee sekä `EV/EBIT`- että `EV/EBITDA`-laskentaa (käyttäen `EBITDA`- tai `Normalized EBITDA` -rivejä, jos `EBIT` puuttuu tai jos `enterpriseToEbitda` on saatavilla) ja tallentaa kentät `current_ev_ebit`, `ev_ebit_5y_median`, `ev_ebit_premium_pct` ja `is_historically_expensive`.
  2. **Tiedosto `screener/tiered_filters.py` (`passes_nordic_midcap_compounder`, rivit 250–260):**
     - Lisää arvostustarkistus: jos `hard_facts.get("reject_historically_expensive", False)` on päällä (tai jos `ev_ebit_premium_pct > 50.0` eli yli +50 % yli 5v mediaanin, tai absoluuttinen `current_ev_ebit > 30.0`), hylkää Core-kandidaatti syyllä `"REJECTED_VALUATION: EV/EBIT exceeds historical 5y valuation ceiling"`.
     - Pidä `+20 % ... +50 %` preemiolla voimassa ankara `-20.0` pisteen `VALUATION PENALTY` (`main_controller.py:2254`), jolloin edullisemmat laatuyhtiöt ohittavat kalliit yhtiöt rankingissa.
- **Validointi (`pytest`):**
  - Testaa Core-yhtiöllä, jonka 5v mediaani EV/EBIT on `12.0` ja nykyinen `15.5` (+29,2 % $\rightarrow$ `is_historically_expensive = True`, -20p rangaistus) vs. nykyinen `20.0` (+66,7 % $\rightarrow$ hylkäys Core-portilla).

---

## Vaihe 1: Kulut ja T+1 Toteutus (Mekaniikan oikaisu)

*(Huom: TASK-01 0,25 % FX-kulut, TASK-02 9 €/15 € minimipalkkiot, TASK-03 Partial TP -kululoukun esto ja TASK-04 AOT-verovaraus on jo toteutettu commitissa `2d72294`.)*

### `[TASK-05 & TASK-07]` T+1 Avausgapit, Rehellinen Slippage ja `Typical Price` (ent. `vwap`)
- **Tavoite:**
  1. Oikaistaan P13-paperisalkun tila vastaamaan päivän $T+1$ todellista täyttymistä (perutaan `UNFILLED_GAP_UP`-haamuostot ja täsmäytetään avauskurssit).
  2. Yhtenäistetään live-kontrollerin ja backtesterin stop-toteutus muotoon `min(Open, trailing_stop)` päivän `Low`-arvoa vasten.
  3. Muutetaan päiväbaarien viitehinta rehellisesti nimelle `typical_price` $\left(\frac{H+L+C}{3}\right)$, koska aitoa volyymipainotettua intraday-VWAPia ei EOD-päivädatasta saa.
- **Tarkka toteutusohje:**
  1. **Tiedosto `main_controller.py` (rivit 1060–1085 & 2948–2962):**
     - Vaihda `momentum_cache[ticker]`-sanakirjaan ensisijaiseksi avaimeksi `"typical_price": typical_px` (pidä `"vwap": typical_px` vain taaksepäinyhteensopivana alias-avaimena vanhoille testeille).
     - Päivitä P12:n lokiviesti (rivi 2956) muotoon: `Ref Typical Price (HLC/3) {ref_px:.2f} +{limit_buf*100:.1f}%`.
  2. **Tiedosto `main_controller.py` (`execute_phase1_exits`, rivit 1765–1790):**
     - Hae position päivän `day_low` ja `day_open` `momentum_cache`-välimuistista (jos saatavilla).
     - Kun salkun trailing stop (`stop_price = cur_peak * (1.0 - trailing_stop_pct)`) alittuu päivän aikana (`day_low <= stop_price`), laukaise myynti ja aseta toteutushinnaksi `fill_exit_px = min(day_open, stop_price)` (jos `day_open > 0`, muuten `min(current_price, stop_price)`).
  3. **Tiedosto `main_controller.py` (`audit_p13_next_day_executions`, rivit 3380–3450):**
     - Lisää `PortfolioConfig`-luokkaan lippu `reconcile_p13_next_day_fills: bool = False` (voidaan aktivoida `portfolios_config.yaml`:ssa `P13_Live_Candidate`-salkulle).
     - Kun `reconcile_p13_next_day_fills` on `True`:
       - Jos `would_have_filled == "UNFILLED_GAP_UP"`: poista toteutumaton positio `portfolio.positions`-listasta ja palauta sen sitoma pääoma (`Capital Invested` + kulut tilivaluutassa) takaisin `portfolio.cash_balance`-saldoon.
       - Jos `would_have_filled == "FILLED"` ja `realistic_fill_val != paper_price`: päivitä position `"Buy Price": realistic_fill_val` ja oikaise `portfolio.cash_balance` erotuksella.
- **Validointi (`pytest`):**
  - Testaa P12:n päivänsisäinen gap-down (`Peak = 10.0`, `6 % stop = 9.40`, seuraavan päivän `Open = 8.80, Low = 8.50, Close = 9.50`) $\rightarrow$ varmista, että stop laukeaa hintaan `8.80`.

---

## Vaihe 2: Kassa, Runway ja Stop-Loss -Hierarkia (Turvallinen avaaminen)

### `[TASK-12]` Kassavirtamoottorin täydellinen korjaus (TTM OCF miinus 4Q CapEx molemmissa putkissa)
- **Tavoite:** Varmistetaan, että vapaa kassavirta (`FCF`) ja kassanpoltto (`Cash Burn`) lasketaan **aina** samalla 12 kuukauden (4Q TTM) aikajänteellä ennen kuin Satelliitti 2 avataan käänneyhtiöille.
- **Nykytilan puute:** Vaikka `get_hard_financials()`-funktiota päivitettiin, tiedoston `screener/financial_metrics_engine.py` aiempi funktio (`calculate_survival_metrics`, rivit 310–325) hakee edelleen `capex = _extract_metric_from_df(cf_q, _CAPEX_KEYS)` (vain 1 kvartaali!). Lisäksi 4 kvartaalin CapEx-summauksessa täytyy ottaa jokaisen kvartaalin itseisarvo (`series.iloc[:4].abs().sum()`), jotta eri etumerkkikäytännöt eivät kumoa toisiaan.
- **Tarkka toteutusohje:**
  1. **Tiedosto `screener/financial_metrics_engine.py` (rivit 310–325 JA rivit 615–630):**
     - Luo yhteinen apufunktio `_extract_ttm_capex(cf_q, cf_a, ocf_source: Optional[str]) -> Optional[float]`:
       ```python
       def _extract_ttm_capex(cf_q: Optional[pd.DataFrame], cf_a: Optional[pd.DataFrame], ocf_source: Optional[str]) -> Optional[float]:
           """Laskee 4 kvartaalin TTM CapExin (itseisarvoina), kun OCF on TTM-tasolla."""
           if ocf_source == "ttm_quarterly" and cf_q is not None and not cf_q.empty:
               for key in _CAPEX_KEYS:
                   if key in cf_q.index:
                       series = cf_q.loc[key].dropna()
                       if len(series) >= 4:
                           return float(series.iloc[:4].abs().sum())
           capex_a = _extract_metric_from_df(cf_a, _CAPEX_KEYS)
           if capex_a is not None:
               return abs(float(capex_a))
           capex_1q = _extract_metric_from_df(cf_q, _CAPEX_KEYS)
           if capex_1q is not None:
               mult = 4.0 if ocf_source in ("ttm_quarterly", "latest_annual") else 1.0
               return abs(float(capex_1q)) * mult
           return None
       ```
     - Käytä `_extract_ttm_capex`-funktiota sekä `calculate_survival_metrics`- että `get_hard_financials`-funktioissa: `fcf = ocf_val - capex_ttm`.
     - Laske `cash_runway_months` (kun `ocf_val < 0` tai `fcf < 0`) käyttäen vuotuista kassanpolttoa `annual_burn = abs(min(ocf_val or 0.0, fcf or 0.0))` suhteessa `adjusted_cash`-kassaan (`Cash - Current_Debt - Current_Lease_Obligation`).

---

### `[TASK-10]` Satelliitti 2:n (`Nordic_MicroCap_Catalyst`) turvallinen avaaminen (`WATCH_TURNAROUND`)
- **Tavoite:** Avataan P13 Satelliitti 2 aidosti käänneyhtiöille (`WATCH_TURNAROUND`) vasta, kun `[TASK-12]` varmistaa, että kassanpoltto ja `adjusted_cash` ovat todellisuuspohjaisia.
- **Tarkka toteutusohje:**
  1. **Tiedosto `screener/tiered_filters.py` (`passes_nordic_microcap_catalyst`, rivit 425–475):**
     - Vaadi `WATCH_TURNAROUND`-polulla **kaikki kolme ehtoa yhtä aikaa**:
       1. `hard_facts.get("runway_status") == "RUNWAY_OK"` ja `float(cash_runway_months) >= 12.0` (laskettuna TASK-12:n korjatulla `adjusted_cash`- ja TTM-kassanpolttolaskurilla; jos kassadata puuttuu `None`, hylkää!).
       2. Vahvistettu katalyytti (`catalyst_ok == True`: uutiskatalyytti, johdon sisäpiiriosto tai kvartaalikäänne).
       3. Ei osakediluutioliputusta (`hard_facts.get("dilution_flag") != "DILUTION_WARNING_RECENT_ISSUE"`).
  2. **Tiedosto `main_controller.py` (rivit 2160–2175):**
     - Pidä `Cash Burner Guard` päällä kaikille muille koreille, ja salli poikkeus `satellite_nordic_microcap_catalyst`-korissa vain yllä olevat 3 ehtoa täyttäville käänneyhtiöille.

---

### `[TASK-09]` Stop-Loss Harmonisaatio (`2.5x ATR` -20 % katolla & `-3.5 % PROTECTIVE_LIMIT`)
- **Tavoite:**
  1. Toteutetaan P13:n dokumentoitu `2.5x ATR` dynaaminen stop-loss ja asetetaan sille **ehdoton maksimikatto `-20.0 %`** (`max_stop_loss_pct = 0.20`).
  2. Muutetaan EOD-pakkomyynnit (`FULL_EXIT` / Stop-Loss / Breakdown) käyttämään `-3.5 %` suojalimiittiä (`PROTECTIVE_LIMIT = Close * 0.965` tai `Bid`, kumpi on alempi/turvallisempi) aiemman `-0.2 %` / `-1.5 %` sijaan, jotta myyntitoimeksianto menee oikeasti läpi epälikvidin micro-capin avauksessa.
- **Tarkka toteutusohje:**
  1. **Tiedosto `main_controller.py` (`execute_phase1_exits`, rivit 1525–1535 ja 1690–1735):**
     - Laske 14 päivän `atr_pct` osakkeen kurssihistoriasta (tai hae `momentum_cache[ticker].get("atr_pct")`).
     - Jos `atr_pct` on saatavilla ja `> 0`:
       - `atr_stop_pct = min(0.20, max(0.12, 2.5 * atr_pct))` (rajattu välille `12,0 % ... 20,0 %`, ehdoton katto `-20,0 %`).
       - Muussa tapauksessa käytä oletusta `0.15` (`-15,0 %`).
     - Aseta `stop_threshold = round(highest_close * (1.0 - atr_stop_pct), 4)`.
  2. **Tiedostot `main_controller.py` (rivit 1835–1845), `email_generator.py` (rivit 222–245) ja `portfolios_config.yaml` (rivi 184):**
     - Lisää `portfolios_config.yaml`-tiedostoon `protective_sell_limit_buffer_pct: 0.035` (`-3.5 %`).
     - Kun `action == "SELL"` (täysi irtautuminen / stop-loss), laske `sell_limit_px = round(current_price * (1.0 - 0.035), 4)` (tai `min(bid_px, round(current_price * 0.965, 4))`, jos `bid_px` on saatavilla) ja merkitse ohjeeksi `"PROTECTIVE_LIMIT (Close -3.5%)"`.
     - Pidä `PARTIAL_TP`-voitonkotiutuksissa tiukka `-0.2 %` limiitti.

---

## Vaihe 3: Markkinaregiimi ja Salkkutason Hätäjarrut (Makro & Rakenne)

### `[TASK-13]` Indeksikohtaiset Regiimisuodattimet (`^OMXH25`, `^OMXSPI` SMA200 hystereesillä & `IWC`)
- **Tavoite:** Lopetetaan pelkän yhdysvaltalaisen `IWC`-ETF:n (SMA50) käyttö pohjoismaisten osakkeiden regiimisuodattimena. Rakennetaan markkinakohtaiset regiimisuodattimet:
  - **Suomi (`.HE`):** `^OMXH25` (SMA200 $\pm 1{,}0\,\%$ hystereesillä)
  - **Ruotsi (`.ST`):** `^OMXSPI` (SMA200 $\pm 1{,}0\,\%$ hystereesillä)
  - **USA (US micro-cap):** `IWC` (SMA200 / SMA50 $\pm 1{,}0\,\%$ hystereesillä)
- **Tarkka toteutusohje:**
  1. **Tiedosto `main_controller.py` (`MarketDataEngine.refresh_data`, rivit 915–960):**
     - Lisää apumetodi `_compute_index_regime(ticker_sym: str, prev_regime: str = "BULL", hysteresis_pct: float = 0.01) -> Dict[str, Any]`:
       - Hakee `1y` (tai `250d`) historian (`yfinance`).
       - Laskee `sma_200` (jos rivejä $< 200$, fallback `sma_50`) ja `last_price`.
       - **Hystereesilogiikka (estää edestakaisen sahaamisen SMA200-viivalla):**
         - Jos `prev_regime == "BULL"`, regiimi vaihtuu `"BEAR / CHOPPY"`-tilaan vasta, kun `last_price < sma_200 * (1.0 - hysteresis_pct)` (eli $-1{,}0\,\%$ alle SMA200).
         - Jos `prev_regime != "BULL"`, regiimi palaa `"BULL"`-tilaan vasta, kun `last_price > sma_200 * (1.0 + hysteresis_pct)` (eli $+1{,}0\,\%$ yli SMA200).
     - Tallenna `self.regional_regimes = {"FI": ..., "SE": ..., "US": ...}` sekä säilytä `self.market_regime` taaksepäinyhteensopivana.
  2. **Tiedosto `main_controller.py` (`execute_phase2_screening`):**
     - Tarkista ostettavan osakkeen kotimarkkina (`FI`, `SE`, `US`) ja sovella kyseisen markkinan regiimisuodatinta!

---

### `[TASK-14]` Drawdown-hälytyksen koventaminen: `-15 %` Drawdown $\rightarrow$ Salkun `Risk-Off`-tila (Ostot seis & Käteispuskuri 60 %)
- **Tavoite:** Pelkkä lokivaroitus ei suojaa pääomaa. Kun salkun kokonaisarvo (`cur_total_equity`) laskee `-15.0 %` tai enemmän salkun huippuarvosta (`peak_equity_eur`), salkku pakotetaan automaattisesti **`RISK_OFF`**-tilaan:
  1. **Uudet ostot pysäytetään** (`sub_block_reason = "PORTFOLIO_DRAWDOWN_RISK_OFF"`).
  2. **Käteispuskurivaatimus nousee 60 prosenttiin** (`min_required_cash = baseline_equity * 0.60`).
- **Tarkka toteutusohje:**
  1. **Tiedosto `main_controller.py` (`PortfolioInstance` & `execute_phase2_screening`, rivit 2615–2680):**
     - Tallenna `peak_equity_eur` ja `risk_off_mode: bool` pysyvästi salkun `*_state.json`-tiedostoon (`load_state` & `save_state`), jotta huippuarvo säilyy daemonin uudelleenkäynnistysten yli!
     - Laske `port_dd_pct = ((cur_total_equity - peak_eq) / peak_eq) * 100.0`.
     - Jos `port_dd_pct <= dd_alert` (oletus `-15.0 %`):
       - Aseta `portfolio.risk_off_mode = True`.
       - Nosta `buffer_pct = max(buffer_pct, 0.60)` (`min_required_cash = baseline_equity * 0.60`).
       - Estä uudet ostot (`return 0` ja aseta `portfolio.last_screening_block_reason = f"RISK_OFF_DRAWDOWN ({port_dd_pct:.1f}% <= {dd_alert:.1f}%)"`).
     - Hystereesi palautumiselle: `portfolio.risk_off_mode` poistuu (`False`), kun salkun drawdown elpyy yli `-10.0 %` tason (tai kun käteistä lisätään/positioita suljetaan niin, että salkku on jälleen turvarajan yläpuolella).

---

### `[TASK-11]` Markkina-arvopohjainen luokittelu Nordnet CSV -tuonnissa (`core/portfolio.py`)
- **Tavoite:** Korjataan Nordnet CSV -tuonti (`_sync_to_p13_state`) luokittelemaan pohjoismaiset yhtiöt **todellisen markkina-arvon (EUR)** perusteella pelkän `.HE`/`.ST`-päätteen tai staattisen CSV-listan sijaan.
- **Tarkka toteutusohje:**
  1. **Tiedosto `core/portfolio.py` (`_sync_to_p13_state`, rivit 280–315):**
     - Luo apufunktio `_resolve_nordic_sub_strategy(sym_up: str, microcap_mcap_map: Dict[str, float]) -> str`:
       - Tarkistaa ensin markkina-arvon paikallisesta metatiedosta (`clean_microcap_universe.csv`:n `market_cap_usd` / `market_cap_eur`) tai `yfinance.Ticker(sym_up).fast_info.get("market_cap")` (muunnettuna euroiksi: SEK `/ 11.3`, USD `/ 1.08`).
       - **Jos markkina-arvo $\ge 150\,000\,000\text{ €}$ (150 M€):** luokittele `"core_nordic_midcap_quality"` (sekä `.HE`- että `.ST`-osakkeille!).
       - **Jos markkina-arvo $< 150\,000\,000\text{ €}$ (tai yhtiö on `microcap_set`-listalla):** luokittele `"satellite_nordic_microcap_catalyst"`.
- **Validointi (`pytest`):**
  - Tuo CSV:stä suomalainen micro-cap (`MCAP = 35 M€`, `.HE`) $\rightarrow$ saa korin `satellite_nordic_microcap_catalyst`.
  - Tuo CSV:stä ruotsalainen mid-cap (`MCAP = 450 M€`, `.ST`) $\rightarrow$ saa korin `core_nordic_midcap_quality`.

---

## Toteutuksen Tarkistuslista (The Hardened Path Checklist)

### Vaihe 0: Datan Laatu ja Arvostus (Suojamuurit)
- [x] **`[TASK-00]` Cross-Sectional Data Audit:** Ajetaan globaalisti koko universumille ennen salkkujen skannausta; `DATA_DUPLICATE_WARNING` asettaa karanteenin (`REJECTED_DATA_LEAK`) ja `> 100 %` kasvut merkitään `MANUAL_REVIEW_OUTLIER`-lipulla. Varmennettu testeissä: `tests/test_data_audit_and_runway.py`.
- [x] **`[TASK-00B]` Core EV/EBITDA (EV/EBIT) vs. 5v historia:** Kova arvostuskatto (+50 % yli 5v mediaanin tai absoluuttinen EV/EBIT > 30.0) ja suhteellinen 5v mediaanisuodatin kytketty `passes_nordic_midcap_compounder`-seulaan. Varmennettu testeissä: `tests/test_tiered_filters.py`.

### Vaihe 1: Kulut ja T+1 Toteutus (Mekaniikka)
- [x] **`[TASK-01..04]` Kulut, FX ja Partial TP:** 0,25 % FX-kulu, 9 €/15 € minimipalkkiot ja Partial TP -kokoraja (valmis commitissa `2d72294`).
- [x] **`[TASK-05 & 07]` T+1 Avausgapit, `min(Open, stop)` & `typical_price`:** Päiväkynttilän viitehinta nimetty rehellisesti `typical_price`:ksi (alias `vwap`), stop-loss laukeaa rehelliseen tasoon `min(cur_open, stop_px)` suojaamaan gap-downeilta. Varmennettu testeissä: `tests/test_backtest_engine.py`.

### Vaihe 2: Kassa, Runway ja Stop-Loss -Hierarkia
- [x] **`[TASK-12]` TTM OCF miinus 4Q CapEx:** Yhtenäinen `_extract_ttm_capex` (itseisarvojen 4Q-summa) molemmissa laskentafunktioissa (`calculate_survival_metrics` ja `get_hard_financials`). Varmennettu testeissä: `tests/test_data_audit_and_runway.py`.
- [x] **`[TASK-10]` Satelliitti 2 (`WATCH_TURNAROUND`):** Avattu tiukasti vain yhtiöille, joilla TASK-12:n mukainen `adjusted_cash` runway $\ge 12\text{ kk}$, vahvistettu katalyytti ja ei diluutioliputusta (> 5 %). Varmennettu testeissä: `tests/test_tiered_filters.py`.
- [x] **`[TASK-09]` Stop-Loss Harmonisaatio:** `2.5x ATR` dynaaminen stop `-20 %` ehdottomalla katolla ja EOD-myynnit `-3.5 %` suojalimiitillä (`PROTECTIVE_LIMIT`). Varmennettu: `email_generator.py` ja `main_controller.py`.

### Vaihe 3: Markkinaregiimi ja Salkkutason Hätäjarrut
- [x] **`[TASK-13]` Indeksikohtaiset Regiimisuodattimet:** Suomi `^OMXH25` (SMA200 $\pm 1{,}0\,\%$ hystereesillä), Ruotsi `^OMXSPI` (SMA200 $\pm 1{,}0\,\%$ hystereesillä) ja USA `IWC`. Varmennettu testeissä: `tests/test_quant_analytics.py`.
- [x] **`[TASK-14]` `-15 %` Drawdown Risk-Off -tila:** Pysyvä `peak_equity_eur`-seuranta; `-15 %` drawdown pysäyttää uudet ostot (`BLOCKED_BY_DRAWDOWN_RISK_OFF`) ja nostaa käteispuskurivaatimuksen 60 prosenttiin. Varmennettu testeissä: `tests/test_multi_portfolio.py`.
- [x] **`[TASK-11]` Markkina-arvopohjainen CSV-tuonti:** Nordnet-tuonti lajittelee pohjoismaiset osakkeet 150 M€ markkina-arvorajan perusteella Core- vs. Satelliittikoriin pelkän kansallistunnuksen sijaan (`core/portfolio.py`).

---

## Toteutuksen Tila & Testitulokset
Kaikki Vaiheiden 0–3 tehtävät on toteutettu ja auditoitu. Testipatteristo ajettu onnistuneesti läpi:
**420 passed, 0 failed** (165.93 s).
