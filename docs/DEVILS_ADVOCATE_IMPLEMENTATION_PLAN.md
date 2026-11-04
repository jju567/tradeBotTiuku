# Koodibotin Toteutussuunnitelma: Devil's Advocate -löydösten Korjaukset

**Päivämäärä:** 4.11.2026  
**Pohjadokumentti:** [docs/DEVILS_ADVOCATE_AUDIT.md](DEVILS_ADVOCATE_AUDIT.md)  
**Kohdeympäristö:** `C:\Users\Jarmo\Documents\kode\trade\tradeBotTiuku` (⚠️ **KRIITTINEN SÄÄNTÖ:** Kaikki kehitys ja testaus tehdään vain `C:\`-asemalla. Älä koskaan ylikirjoita `Z:\`-tuotantoasemaa.)

---

## Yleisohje Koodibotille (Execution Protocol)

1. **Vaiheistus:** Toteuta tehtävät (`TASK-01` – `TASK-15`) alla määritellyssä järjestyksessä. Jokainen tehtävä on itsenäinen, atomisesti testattava kokonaisuus.
2. **Taaksepäinyhteensopivuus:** Älä riko olemassa olevien `data/portfolios/portfolio_*_state.json`-tiedostojen latausta. Käytä aina `.get("avain", oletusarvo)`-hakua uusille tilakentille.
3. **Testaus:** Aja jokaisen vaiheen jälkeen `pytest` (sekä lopuksi `python run_all_tests.py`) varmistaaksesi, ettei mikään olemassa oleva testi hajoa.
4. **Dokumentaatio & Git:** Päivitä `README.md` ja `docs/OSAKEPOIMINTALOGIIKKA.md` vastaamaan uutta logiikkaa ja vie muutokset gittiin (`git add`, `git commit`, `git push origin main`).

---

## Vaihe 1: Kululaskennan, Valuuttakulujen (FX) ja Verotuksen Korjaukset

### TASK-01: OST/AOT 0,25 % valuutanvaihtokulun (FX Fee) vähentäminen käteisestä ja nettotuotosta
- **Audit-viite:** Kohta 2.1 (🔴 Kriittinen vuoto)
- **Kohdetiedosto:** `main_controller.py`
- **Ongelma:** `est_fx_fee_eur` (0,25 % suuntaansa ulkomaisissa valuutoissa) lasketaan vain metatietoihin (`Estimated_FX_Fee_EUR`), mutta sitä ei vähennetä `cash`-saldosta ostoissa/myynneissä eikä `Net PnL` -laskennassa. Lisäksi salkut P1–P12 eivät vähennä FX-kuluja lainkaan.
- **Toteutusohje:**
  1. Luo `main_controller.py`-tiedostoon (luokkaan `PortfolioEngine`, esim. rivin 315 jälkeen) apumetodi valuutanvaihtokululle:
     ```python
     @staticmethod
     def _calculate_fx_fee_acc(gross_amount_acc: float, trade_curr: str, acc_curr: str = "EUR") -> float:
         """Laskee Nordnetin 0.25% valuutanvaihtokulun (OST/AOT), jos kaupan valuutta poikkeaa tilin valuutasta."""
         if not trade_curr or trade_curr.upper() == acc_curr.upper():
             return 0.0
         return round(abs(gross_amount_acc) * 0.0025, 2)
     ```
  2. **Myynnit (`execute_phase1_exits`, rivit 1525–1535 Partial TP ja rivit 1785–1800 Full Exit):**
     - Laske `fx_fee_acc = self._calculate_fx_fee_acc(gross_return_acc, curr, acc_curr)` **kaikille salkuille** (P1–P13).
     - Vähennä `fx_fee_acc` suoraan nettotuloksesta:
       ```python
       net_return_acc = gross_return_acc - fee_acc - fx_fee_acc
       ```
     - Vähennä sekä oston että myynnin FX-osuus position `net_pnl_acc`-laskelmasta.
  3. **Ostot (P13 rivit 2338–2342, P12 rivit 2875–2885, P1–P11 rivit 3055–3065):**
     - Laske `est_fx_fee_eur = self._calculate_fx_fee_acc(actual_gross_buy * fx_to_acc, curr, acc_curr)`.
     - Lisää `est_fx_fee_eur` suoraan `total_cost_acc`-muuttujaan:
       ```python
       total_cost_acc = (actual_gross_buy * fx_to_acc) + fee_acc + est_fx_fee_eur
       ```
     - Tarkista, että `total_cost_acc <= cash_balance` (jos ylittyy, pienennä `shares`-määrää yhdellä osakkeella).
- **Validointi:** Kirjoita yksikkötesti, jossa ostetaan ja myydään USD-määräinen osake samalla kurssilla: varmista, että `cash` pienenee täsmälleen `2 * fee_acc + 2 * fx_fee_acc`.

---

### TASK-02: Markkinakohtaisten välityspalkkioiden yhtenäistäminen (`config.py` -> `main_controller.py` & `institutional_backtester.py`)
- **Audit-viite:** Kohta 2.2 (🔴 Kriittinen vuoto)
- **Kohdetiedostot:** `main_controller.py` (rivit 271, 310–315) ja `scripts/institutional_backtester.py` (rivi 103)
- **Ongelma:** `main_controller.py` kovakoodaa `MIN_BROKER_FEE = 9.00` € kaikille pörsseille, vaikka `config.py` (`calculate_transaction_fee`, rivit 81–104) määrittelee ulkomaisille pörsseille (US, `.ST`, `.DE`, `.L`) 15,00 € minimipalkkion perustasoilla. Backtester käyttää vain 0,50 % kokonaiskitkaa.
- **Toteutusohje:**
  1. Päivitä `PortfolioEngine._calculate_fee` (`main_controller.py:310–312`) ottamaan vastaan valinnainen `ticker: str = ""` (tai pörssipääte):
     ```python
     @staticmethod
     def _calculate_fee(gross_eur: float, ticker: str = "") -> float:
         if gross_eur <= 0:
             return 0.0
         t_up = (ticker or "").upper()
         # Nordnet hinnasto: Helsinki (.HE) min 9.00 EUR, ulkomaat (US, .ST, .CO, .OL, .DE) min 15.00 EUR (tai config.py mukaan)
         min_fee = 9.00 if t_up.endswith(".HE") else 15.00
         pct_fee = 0.0020
         return round(max(min_fee, gross_eur * pct_fee), 2)
     ```
  2. Päivitä kaikki `self._calculate_fee(...)`-kutsut tiedostossa `main_controller.py` välittämään `ticker` toisena argumenttina.
  3. Päivitä `scripts/institutional_backtester.py` (rivi 103 ja tuottolaskenta rivi 310–335):
     - Lisää parametrina `position_size_eur: float = 1500.0` ja laske todellinen prosentuaalinen kulurasite: `fee_drag_pct = ((2 * min_fee_eur) / position_size_eur) * 100.0 + fx_roundtrip_pct + spread_slippage_pct`.
- **Validointi:** Varmista, että `.HE`-osakkeen minimipalkkio on 9,00 € ja US/`.ST`-osakkeen 15,00 €, ja että P13-kokonaissääntö (`fee_acc / gross_buy_eur <= 0.02`) estää liian pienet ulkomaiset ostot.

---

### TASK-03: Partial Take-Profit -kululoukun esto pienissä positioissa (Minimi-kokoraja osittaismyynnille)
- **Audit-viite:** Kohta 2.3 (🟠 Optimistinen oletus / Kululoukku)
- **Kohdetiedosto:** `main_controller.py` (rivit 1490–1550, `execute_phase1_exits`)
- **Ongelma:** P1–P4-salkuissa (positio 790 €) puolikkaan position (~493 €) myyminen +25 % kohdalla aiheuttaa kolinkertaisen minimipalkkion (Osto + Partial Sell + Final Sell = 27–45 €).
- **Toteutusohje:**
  - Lisää `execute_phase1_exits`-funktion Partial Take-Profit -lohkoon (rivi 1500) kulutehokkuusehto ennen osittaismyyntiä:
    ```python
    partial_gross_acc = (shares_to_sell * current_price) * fx_to_acc
    partial_fee_acc = self._calculate_fee(partial_gross_acc, ticker)
    remaining_gross_acc = ((shares - shares_to_sell) * current_price) * fx_to_acc
    # Jos osittaismyynnin kulu ylittää 1.5% myyntisummasta tai jäljelle jäävä positio on alle 750 EUR, jätä osittaismyynti tekemättä (anna koko position juosta trailing stopin varassa)
    if partial_fee_acc / max(partial_gross_acc, 1e-9) > 0.015 or remaining_gross_acc < 750.0:
        logging.info(f"[{p_id}] ⏭️ SKIPPED Partial TP for {ticker}: Position too small for cost-efficient split (Gross {partial_gross_acc:.0f} EUR, Fee {partial_fee_acc:.2f} EUR).")
    ```
- **Validointi:** Testaa 790 € positiolla, että Partial TP ohitetaan kulutehottomana, mutta ≥ 1 580 € positiolla se suoritetaan normaalisti.

---

### TASK-04: AOT / OY -veroprofiilin alkupääoman korjaus ja myyntivoittoveron vähennys
- **Audit-viite:** Kohta 2.4 (🟡 Rakenteellinen ristiriita)
- **Kohdetiedosto:** `main_controller.py` (rivit 1325–1362 ja 1790–1815)
- **Ongelma:** Rivi 1330 kovakoodaa `11 250 €` alkupääoman AOT-tilassa (olettaen 10 000 € salkun 7 900 € sijaan), eikä AOT/OY-tilassa vähennetä luovutusvoittoveroa voitollisten kauppojen realisoinnissa.
- **Toteutusohje:**
  1. Korvaa rivin 1330 kovakoodattu `11250.0` dynaamisella suhteellisella skaalauksella: `p_cfg.start_cash = round(p_cfg.start_cash * 1.125, 2)` (jos halutaan kompensoida pienempää salkkumäärää) tai pidä `portfolios_config.yaml`-tiedoston `start_cash` muuttumattomana.
  2. Lisää `execute_phase1_exits`- ja Partial TP -myynteihin: jos `self.account_tax_profile == "AOT"` ja `net_pnl_acc > 0`, tallenna tai vähennä laskennallinen verovaraus (`tax_reserve_acc = round(net_pnl_acc * 0.30, 2)` AOT:lle, `0.20` OY:lle) tilatiedoston `tax_liability_eur`-kenttään ja huomioi se nostettavissa olevassa käteisessä.

---

## Vaihe 2: Toimeksiantojen Toteutuksen, Rajahintojen ja Stop-Lossien Korjaukset

### TASK-05: P13 Paperisalkun $T+1$ OHLC-täsmäytys (`audit_p13_next_day_executions` -> Todellinen salkkutila)
- **Audit-viite:** Kohta 1.1 (🔴 Kriittinen vuoto)
- **Kohdetiedosto:** `main_controller.py` (rivit 2310–2420 ja 3167–3345)
- **Ongelma:** P13 kirjaa ostot ja myynnit suoraan päivän $T$ päätöskurssiin, vaikka `audit_p13_next_day_executions` havaitsee seuraavana päivänä ($T+1$), että kauppa jäi toteutumatta (`UNFILLED_GAP_UP` / `UNFILLED_GAP_DOWN`) tai toteutui avauskurssilla (`FILLED_AT_OPEN`).
- **Toteutusohje:**
  1. Kun `audit_p13_next_day_executions` (`main_controller.py:3167`) käy läpi `pending_execution_audits`-jonon seuraavana päivänä ($T+1$):
     - **Jos `action == "BUY"` ja `fill_status == "UNFILLED_GAP_UP"`:**
       - Peru "haamuosto" `P13_Live_Candidate`-salkun `holdings`-listasta! Palauta ostossa veloitettu `total_cost_acc` takaisin `state["cash"]`-saldoon ja kirjaa lokiin: `[P13 RECONCILE] 🔄 Reverted unfilled BUY for {ticker} (Next-day Open {open_px} > Limit {limit_px})`.
     - **Jos `action == "BUY"` ja `fill_status in ("FILLED_AT_OPEN", "FILLED_INTRADAY")`:**
       - Päivitä position `Buy Price` vastaamaan todellista `actual_fill_px`-toteutushintaa ja oikaise `state["cash"]`-erotus `(cand_price - actual_fill_px) * shares * fx_to_acc`.
     - **Jos `action in ("SELL", "PARTIAL_TP")` ja `fill_status in ("FILLED_AT_OPEN", "FILLED_INTRADAY")`:**
       - Oikaise `state["cash"]`-saldo vastaamaan todellisen toteutushinnan (`actual_fill_px`) ja eilisen päätöskurssin välistä erotusta.
- **Validointi:** Kirjoita testi, jossa P13 ostaa osakkeen EOD-hintaan 10,00 € (limiitti 10,02 €), ja seuraavan päivän OHLC on `Open=10.50, Low=10.40`. Varmista, että `audit_p13_next_day_executions` palauttaa rahat ja poistaa osakkeen P13-salkusta.

---

### TASK-06: Stop-Loss ja Hätämyyntien (`FULL_EXIT`) vapauttaminen `-0.2 %` limiittiloukusta
- **Audit-viite:** Kohta 1.2 (🔴 Kriittinen vuoto)
- **Kohdetiedostot:** `email_generator.py` (rivit 175–190, 225–240), `main_controller.py` (rivit 1805–1825, 3255–3275) ja `portfolios_config.yaml` (rivit 182–186)
- **Ongelma:** `FULL_EXIT` (Stop-Loss, `QUARTERLY_FUNDAMENTAL_BREAKDOWN`, `LLM_NEWS_REJECT`) käyttää `LIMIT (Prev Close - 0.2%)` -toimeksiantoa, joka ei toteudu laskevassa avauksessa.
- **Toteutusohje:**
  1. Lisää `portfolios_config.yaml`-tiedoston `p13_execution_rules`-osioon:
     ```yaml
     stop_loss_order_type: "PROTECTIVE_LIMIT"
     stop_loss_limit_buffer_pct: 0.035  # -3.5% suojalimiitti pakkomyynneille (varmistaa täytön ilman pohjatonta market-myyntiä)
     ```
  2. Muuta `email_generator.py` (rivi 180–190):
     - Jos `action == "SELL"` (täysi exit / stop-loss / breakdown), käytä `-3.5 %` suojalimiittiä (`ref_price * (1.0 - 0.035)`) ja `time_in_force = "DAY"`, ja merkitse sähköpostiin selkeästi: `STOP-LOSS / EXIT LIMIT (-3.5% suojapuskuri)`.
     - Pidä tiukka `-0.2 %` rajahinta vain `PARTIAL_TP`-voitonkotiutuksissa, joilla ei ole kiire.
  3. Päivitä vastaava `target_limit_price` `main_controller.py`:n riviin 1812 (`pending_execution_audits` myynneille).

---

### TASK-07: P12 "Smart Limit" -tautologian korjaus ja päivänsisäisen Stop-Loss-vuodon paikkaus
- **Audit-viite:** Kohdat 1.3 ja 1.4 (🔴 Kriittiset vuodot)
- **Kohdetiedosto:** `main_controller.py` (rivit 1005–1040, 1650–1760 ja 2840–2860)
- **Ongelma:**
  1. `momentum_cache` ei sisällä `vwap`- tai edellisen kynttilän hintaa, jolloin `cand_price > target_limit` on aina `False`.
  2. P12 (ja muiden salkkujen) trailing stop tarkistetaan vain `Close`-hinnasta (`current_price`), ei päivän `Low`/`Open`-hinnoista, jolloin päivänsisäiset romahdukset vuotavat kymmeniä prosentteja.
- **Toteutusohje:**
  1. **Momentum Cache (`main_controller.py:1010–1035`):**
     - Laske 5 päivän tai päivän tyypillinen VWAP-approksimaatio OHLCV-datasta: `typical_px = ((df['High'] + df['Low'] + df['Close']) / 3.0).iloc[-1]` ja tallenna se `momentum_cache[ticker]["vwap"] = float(typical_px)`.
     - Nyt rivin 2843 `ref_px = cand.get("vwap") or cand_price` vertaa päätöskurssia päivän tyypilliseen hintaan (jos osake sulkee piikkinä kaukana päivän keskihinnan yläpuolella, Smart Limit estää ylihintaisen oston).
  2. **Intraday Low / Gap-Down Stop-Loss (`main_controller.py:1650–1760`):**
     - Hae `execute_phase1_exits`-metodissa kullekin positiolle myös päivän `day_low` ja `day_open` (`self.market_engine.price_cache[ticker]` viimeisin rivi).
     - Jos salkulla on aktiivinen mekaaninen stop-loss-taso (`stop_price = peak_price * (1.0 - trailing_stop_pct)` tai kiinteä stop), tarkista laukesiko se päivän aikana (`day_low <= stop_price`). Jos laukesi, käytä toteutushintana `exit_px = min(day_open, stop_price)` (täsmälleen samoin kuin `scripts/institutional_backtester.py` rivillä 242).

---

## Vaihe 3: Osakepoiminnan, Stop-Loss-hierarkian ja Fundamenttimoottorin Korjaukset

### TASK-08: Kandidaattien paremmuusjärjestys (Ranking) salkkuihin P1–P10 (CSV-rivijärjestysvinouman poisto)
- **Audit-viite:** Kohta 3.1 (🔴 Kriittinen vuoto)
- **Kohdetiedosto:** `main_controller.py` (rivi 2930)
- **Ongelma:** Salkut P1–P10 iteroivat `self.market_engine.universe_metadata.items()`-sanakirjaa CSV-rivijärjestyksessä ja ostavat aina tiedoston alkupään osakkeet (`VIAFIN.HE`, `MOB.ST` jne.).
- **Toteutusohje:**
  - Ennen riviä 2930 (`for ticker, meta in ...`), järjestä kandidaatit deterministiseen paremmuusjärjestykseen:
    1. Ensisijaisesti `Conviction_Score` (laskeva) tai fundamenttiprofiili (`STRONG_BUY` > `WATCH_TURNAROUND`).
    2. Toissijaisesti `fcf_yield` tai `roic` / `rev_yoy` (laskeva) `fundamental_cache`-välimuistista.
    ```python
    sorted_universe = sorted(
        self.market_engine.universe_metadata.items(),
        key=lambda item: (
            1 if (self.market_engine.fundamental_cache.get(item[0]) or {}).get("verdict") == "STRONG_BUY" else 0,
            float((self.market_engine.fundamental_cache.get(item[0]) or {}).get("conviction_score") or 0.0),
            float(((self.market_engine.fundamental_cache.get(item[0]) or {}).get("raw_metrics") or {}).get("fcf") or 0.0)
        ),
        reverse=True
    )
    for ticker, meta in sorted_universe:
    ```
- **Validointi:** Varmista testillä, että CSV:n viimeisellä rivillä oleva korkean Conviction Scoren osake ostetaan ennen CSV:n ensimmäisellä rivillä olevaa matalamman pistemäärän osaketta.

---

### TASK-09: Stop-Loss-ristiriidan ratkaisu (`-8 % / -12 %` vs. Tri-Layer `-50 %` ja P13 `2.5x ATR`)
- **Audit-viite:** Kohdat 3.2 ja 3.3 (🔴 Kriittinen ristiriita & 🟡 Kuollut sääntö)
- **Kohdetiedostot:** `portfolios_config.yaml` (rivit 14–180) ja `main_controller.py` (rivit 345–346, 1660–1740)
- **Ongelma:**
  1. `PortfolioConfig` asettaa kaikille salkuille P1–P11 oletuksena `us_stop_loss_pct: 0.08` ja `nordic_stop_loss_pct: 0.12`, mikä kumoaa `screener/portfolio_manager.py`:n `-50 %` Catastrophic Stopin (mukaan lukien `P3_Diamond_Hands`).
  2. P13:n YAML-konfiguraatiossa lukee `-15.0% (or 2.5x ATR)` ja `-12.0% (or 2.5x ATR)`, mutta `2.5x ATR`-laskentaa ei ole koodissa.
- **Toteutusohje:**
  1. Muuta `PortfolioConfig`-luokan (`main_controller.py:345–346`) oletusarvot tai `portfolios_config.yaml`-määritykset siten, että:
     - `P3_Diamond_Hands` (ja muut Tri-Layer-salkut, joissa halutaan antaa tilaa multibaggereille) käyttävät `us_stop_loss_pct: 0.50` / `nordic_stop_loss_pct: 0.50` (tai erillistä `use_tight_regional_stops: false` -lippua).
  2. Lisää P13-stop-tarkistukseen (`main_controller.py:1665` ja `1701`) `2.5x ATR` dynaaminen tuki:
     - Hae osakkeen 14 päivän `atr_pct` (`price_cache`-datasta).
     - Laske `effective_stop_pct = max(base_stop_pct, min(2.5 * atr_pct, 0.20))` (tai `min(base_stop_pct, 2.5 * atr_pct)` riippuen halutaanko ATR:n toimivan kattona vai volatiliteettipuskurina; dokumentoi valinta selkeästi `portfolios_config.yaml`-tiedostoon).

---

### TASK-10: P13 Satelliitti 2 (`Nordic_MicroCap_Catalyst`) `WATCH_TURNAROUND`-portin avaaminen
- **Audit-viite:** Kohta 3.4 (🔴 Kriittinen looginen umpikuja)
- **Kohdetiedostot:** `screener/tiered_filters.py` (rivit 418–453) ja `main_controller.py` (rivit 2096–2108)
- **Ongelma:** Portti 2b vaatii ehdottomasti `pos_cf == True` (`OCF > 0` tai `FCF > 0`), mikä hylkää 100 % kassavirtanegatiivisista käänneyhtiöistä ja tekee rivin 442 `runway >= 12.0` -ehdosta kuollutta koodia.
- **Toteutusohje:**
  1. Muokkaa `screener/tiered_filters.py` (`passes_nordic_microcap_catalyst`, rivit 418–445):
     - Jos yhtiöllä on `pos_cf == True` JA `prof_a or prof_b`: hyväksy laatu-mikroyhtiönä.
     - Jos yhtiöllä on `verdict == "WATCH_TURNAROUND"` JA vahvistettu katalyytti (`has_catalyst == True`), **salli `pos_cf == False`**, kunhan `runway == "Infinite" or (isinstance(runway, (int, float)) and runway >= 12.0)`!
  2. Muokkaa `main_controller.py` (`Cash Burner Guard`, rivit 2096–2106):
     - Salli P13:n `satellite_nordic_microcap_catalyst`-korille poikkeus `Cash Burner Guard`-säännöstä silloin, kun `verdict == "WATCH_TURNAROUND"`, katalyytti on vahvistettu ja `cash_runway_months >= 12.0`.
- **Validointi:** Lisää yksikkötesti yhtiölle, jolla `OCF = -1.0M`, `Cash = 2.0M` (`runway = 24 kk`), `verdict = "WATCH_TURNAROUND"` ja `catalyst_detected = True`: varmista, että `passes_nordic_microcap_catalyst` palauttaa `True`.

---

### TASK-11: Nordnet CSV -tuonnin (`_sync_to_p13_state`) älykäs alastrategialuokittelu
- **Audit-viite:** Kohta 3.5 (🔴 Kriittinen vuoto)
- **Kohdetiedosto:** `core/portfolio.py` (rivit 280–305, `_sync_to_p13_state`)
- **Ongelma:** `_sync_to_p13_state` sijoittaa kaikki `.HE`-osakkeet sokeasti `core_nordic_midcap_quality`-koriin, jolloin suomalaiset micro-capit pakkomyydään välittömästi, jos `ROE < 8 %` tai `EBIT Margin < 5 %`.
- **Toteutusohje:**
  - Muuta `_sync_to_p13_state` (`core/portfolio.py:286–295`) tarkistamaan osakkeen markkina-arvo ja/tai fundamenttiprofiili ennen `Sub_Strategy`-leiman asettamista:
    ```python
    if ticker.endswith((".HE", ".ST", ".CO", ".OL")):
        # Jos yhtiö löytyy clean_microcap_universe.csv:stä (tai markkina-arvo < 150M EUR / ROE < 8%), luokittele satelliitiksi, ei Core-midcapiksi!
        is_micro = ticker in microcap_tickers
        sub_strat = "satellite_nordic_microcap_catalyst" if is_micro else "core_nordic_midcap_quality"
    else:
        sub_strat = "satellite_us_microcap_growth"
    ```
- **Validointi:** Testaa, että suomalaisen micro-capin (`SSH1V.HE`) tuonti CSV:stä saa alastrategiakseen `satellite_nordic_microcap_catalyst`, eikä laukea `QUARTERLY_FUNDAMENTAL_BREAKDOWN`-pakkomyyntiin.

---

### TASK-12: Fundamenttimoottorin (`financial_metrics_engine.py`) 4Q TTM CapEx, Kvartaalikasvu ja `zero_debt_inferred`-suojaus
- **Audit-viite:** Kohta 3.6 (🔴 Kriittinen vuoto)
- **Kohdetiedosto:** `screener/financial_metrics_engine.py` (rivit 260–275, 314–325, 374–415, 460–470)
- **Ongelma:**
  1. `capex` otetaan yhdestä kvartaalista (`cf_q.iloc[0]`), kun taas `ocf_val` on 4 kvartaalin TTM-summa $\rightarrow$ `FCF` yliarvioidaan.
  2. `get_hard_financials()` lukee `rev_curr`, `rev_prev` ja `gp_val` ensisijaisesti vanhasta vuositilinpäätöksestä (`inc_a`) ja vasta toissijaisesti kvartaaliraportista (`inc_q`).
  3. `zero_debt_inferred` tulkitsee puuttuvat velkarivit nollavelaksi (`debt = 0.0`).
- **Toteutusohje:**
  1. **4Q TTM CapEx (rivit 314–319 & 463–464):**
     - Kun `ocf_period == "ttm_quarterly"` ja `cf_q` sisältää vähintään 4 saraketta, laske myös `capex` 4 viimeisimmän kvartaalin summana! Jos käytetään vain 1 kvartaalia, skaalaa molemmat samalle aikajänteelle.
  2. **Tuorein kvartaalikasvu (YoY) ensisijaiseksi (rivit 374–413):**
     - Jos `inc_q` sisältää vähintään 5 kvartaalia (`inc_q.shape[1] >= 5`), laske YoY-liikevaihdon kasvu vertaamalla tuoreinta kvartaalia (`col 0`) vuoden takaiseen kvartaaliin (`col 4`) – tai 4Q TTM vs. edellinen 4Q – ja käytä vuositilinpäätöstä (`inc_a`) vasta varalla (`fallback`).
  3. **`zero_debt_inferred`-suojaus (rivit 260–273):**
     - Jos taseesta löytyy `Total Liabilities` (tai `Total Assets - Stockholders Equity`), ja korollisen velan rivit puuttuvat, tarkista onko `Total Liabilities / Total Assets > 0.50`. Jos yhtiöllä on korkeat kokonaisvelat mutta `debt`-rivi puuttuu, **älä** päätä `debt = 0.0`, vaan aseta `debt = None` (`debt_missing_high_liabilities`), jotta velkainen yhtiö ei saa vääriä Profile B (`net_cash = True`) -pisteitä.
- **Validointi:** Päivitä ja aja `pytest tests/test_financial_metrics_engine.py`.

---

## Vaihe 4: Riskienhallinnan, Regiimisuodattimen ja NLP-turvaporttien Korjaukset

### TASK-13: Pohjoismainen markkinaregiimisuodatin (`^OMXHPI` / `^OMXSPI`) Nordic-salkuille
- **Audit-viite:** Kohta 4.1 (🟠 Optimistinen oletus / Katvealue)
- **Kohdetiedosto:** `main_controller.py` (rivit 910–940 ja 2910–2925)
- **Ongelma:** Kaikki salkut (myös `P5_Nordic_Only` ja P13:n pohjoismaiset korit) käyttävät vain USA:n `IWC`-ETF:ää regiimisuodattimena.
- **Toteutusohje:**
  - Lataa `MarketDataEngine.refresh_data`-metodissa `IWC`:n rinnalla pohjoismainen vertailuindeksi (esim. `^OMXH25` / `^OMXSPI`).
  - Tallenna `self.is_us_bull_regime` ja `self.is_nordic_bull_regime` erikseen. Käytä pohjoismaisille osakeostoille (`.HE`, `.ST`) `is_nordic_bull_regime`-suodatinta ja US-osakeostoille `is_us_bull_regime`-suodatinta.

---

### TASK-14: Dynaaminen käteispuskuri (`total_equity`) ja Salkkutason `-15 %` Max Drawdown -hätäjarru
- **Audit-viite:** Kohdat 4.2 ja 4.3 (🟡 Rakenteellinen ristiriita / Kuollut parametri)
- **Kohdetiedosto:** `main_controller.py` (rivit 2450–2465 ja 1380–1450)
- **Ongelma:**
  1. `min_required_cash = cfg.start_cash * buffer_pct` (rivi 2453) laskee käteispuskurin kiinteästä alkupääomasta eikä salkun nykyarvosta (`total_equity`).
  2. `portfolio_max_drawdown_alert_pct: -15.0` ei ole kytketty mihinkään.
- **Toteutusohje:**
  1. Muuta rivi 2453 muotoon:
     ```python
     current_total_equity = cash_balance + self._get_open_positions_value_acc(holdings, acc_curr)
     min_required_cash = current_total_equity * buffer_pct
     ```
  2. Tallenna salkun `state`-rakenteeseen `peak_equity_eur = max(state.get("peak_equity_eur", cfg.start_cash), current_total_equity)`.
  3. Laske salkun drawdown: `port_dd_pct = ((current_total_equity - peak_equity_eur) / peak_equity_eur) * 100.0`. Jos `port_dd_pct <= cfg.portfolio_max_drawdown_alert_pct` (`-15.0 %`), kirjaa kriittinen varoitus lokiin ja sähköpostiraporttiin sekä estä uudet satelliittiostot, kunnes drawdown palautuu.

---

### TASK-15: NLP-hallusinaatiosuoja vieraille tickereille (`Target Ticker Consistency Check`)
- **Audit-viite:** Kohta 5.3 (🟠 Empiirinen vuoto: `HUMBLE.ST` pakkomyytiin `SPCX`-uutisesta)
- **Kohdetiedostot:** `screener/nlp_analyzer.py` ja `main_controller.py` (rivit 848–865, 1155–1170)
- **Ongelma:** Kun RSS-uutisotsikossa mainitaan useita yhtiöitä, LLM saattaa hylätä vieraasta tickeristä kertovan uutisen (`SPCX is rejected as an empty shell`), mikä laukaisee kohdeosakkeen (`HUMBLE.ST`) pakkomyynnin.
- **Toteutusohje:**
  1. Lisää `screener/nlp_analyzer.py`-promptiin eksplisiittinen sääntö:
     > *"CRITICAL RULE: Only evaluate facts that directly apply to the target company (`{ticker}`). If a news snippet is a multi-company roundup mentioning other unrelated tickers, ignore the other tickers completely."*
  2. Lisää `main_controller.py`:n NLP-hylkäystarkistukseen (rivit 850 ja 1157) tarkistus: jos `reasoning`-teksti ei mainitse kohdetickeriä (tai yhtiön nimeä) lainkaan, mutta mainitsee jonkin muun isokirjaimisen tickerin yhteydessä `"empty shell"` / `"rejected"`, ohita `REJECT`-tuomio vääriin tickereihin kohdistuvana hallusinaationa.

---

## Koontitaulukko Toteutusjärjestyksestä (Checklist Koodibotille)

- [ ] **TASK-01:** Vähennä 0,25 % FX-kulu `cash`-saldosta ja `Net PnL`:stä ostoissa ja myynneissä (`main_controller.py`).
- [ ] **TASK-02:** Yhtenäistä markkinakohtaiset minimipalkkiot (`.HE` 9 €, ulkomaat 15 €) (`main_controller.py`, `institutional_backtester.py`).
- [ ] **TASK-03:** Estä kulutehoton Partial Take-Profit alle 750 € jäännöspositioissa (`main_controller.py`).
- [ ] **TASK-04:** Korjaa AOT/OY-alkupääoman skaalaus ja lisää luovutusvoittoverovaraus (`main_controller.py`).
- [ ] **TASK-05:** Kytke `audit_p13_next_day_executions` perumaan `UNFILLED_GAP_UP`-haamuostot ja täsmäyttämään $T+1$ avauskurssit P13-tilaan (`main_controller.py`).
- [ ] **TASK-06:** Muuta `FULL_EXIT` (Stop-Loss) -myynnit käyttämään `-3.5 %` suojalimiittiä `-0.2 %` sijaan (`email_generator.py`, `portfolios_config.yaml`).
- [ ] **TASK-07:** Korjaa P12 Smart Limit (`vwap`) ja lisää päivänsisäisen `Low`/`Open`-tason Stop-Loss-tarkistus (`main_controller.py`).
- [ ] **TASK-08:** Lisää kandidaattien paremmuusjärjestys (Ranking) salkkuihin P1–P10 ennen ostosilmukkaa (`main_controller.py`).
- [ ] **TASK-09:** Poista `-8 % / -12 %` pakkostoppi Tri-Layer/Diamond Hands -salkuilta ja toteuta P13 `2.5x ATR` -stop (`main_controller.py`, `portfolios_config.yaml`).
- [ ] **TASK-10:** Salli `WATCH_TURNAROUND`-yhtiöille negatiivinen TTM-kassavirta P13 Satelliitti 2:ssa, kun `runway >= 12 kk` ja katalyytti on vahvistettu (`screener/tiered_filters.py`, `main_controller.py`).
- [ ] **TASK-11:** Korjaa `_sync_to_p13_state` luokittelemaan `.HE` micro-capit satelliittikoriin Core-midcap-korin sijaan (`core/portfolio.py`).
- [ ] **TASK-12:** Korjaa 4Q TTM CapEx, priorisoi kvartaalikohtainen YoY-kasvu ja suojaa `zero_debt_inferred` korkeavelkaisilla yhtiöillä (`screener/financial_metrics_engine.py`).
- [ ] **TASK-13:** Lisää pohjoismainen markkinaregiimisuodatin (`^OMXH25` / `^OMXSPI`) Nordic-ostoille (`main_controller.py`).
- [ ] **TASK-14:** Laske käteispuskuri nykyisestä `total_equity`-arvosta ja aktivoi `-15 %` Max Drawdown -hälytys/jarru (`main_controller.py`).
- [ ] **TASK-15:** Lisää NLP-hallusinaatiosuoja vieraiden tickereiden uutisotsikoille (`screener/nlp_analyzer.py`, `main_controller.py`).
