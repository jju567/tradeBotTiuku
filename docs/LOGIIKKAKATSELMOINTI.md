# Peruslogiikan koodikatselmointi

## Edistyminen

- Osa 1 valmis: salkun riskitarkistukset, tasapainotusehdotukset, portfolio-tila, AI-arviointi ja viikkajonon/markkinamonitorin keskeiset kutsupolut.
- Osa 2 valmis: markkinadatan valuuttayksiköt, indikaattorien rajatapaukset ja CSV-tuonnin otsakekäsittely.
- Osa 3 valmis: screener-salkun exit-säännöt sekä monisalkkudaemonin P12 momentum-ostot ja välimuistinhallinta.
- Kaikki löydökset 1–10 on korjattu ja varmennettu kattavilla yksikkötesteillä.
- Osa 4 valmis: price-fetcher, fundamenttien profilointi ja NLP core -analyysin fallback-polku tarkistettu.
- Osa 5 valmis: dynaaminen universe, web-varmennus ja screeningin päätösrajat tarkistettu.
- Osa 6 valmis: paperisalkun position state, partial/full exit -kirjanpito, FX-muunnos ja equity snapshot tarkistettu; ei uutta varmennettua löydöstä.
- Osa 7 valmis: screenerin sizing, likviditeettisuodatin ja position avaaminen tarkistettu.
- Osa 8 valmis: kesken katselmoinnin tehdyt korjaukset 12–15 tarkistettu ajantasaisesta lähteestä ja runtime-esimerkeillä.
- Osa 9 valmis: Nordic screeningin sizing- ja exit-PnL-valuuttakäsittely tarkistettu.
- Avoinna 3 koodilöydöstä (11, 16 ja 18) sekä 1 rikkoutunut regressiotesti (17), alla.
- Katselmointi ei ole vielä koko sovelluksen kattava.
- Seuraava osa: tarkista paperisalkun entry/exit-toteutuksen valuutta-, fee- ja positionkirjanpito vielä tältä nykyiseltä lähtökohdalta.

## Löydökset ja Korjaukset

1. **[KORJATTU] Korkea: monitori voi kaatua toistuvaan volatiliteettihälytykseen.** `MarketMonitor` tallentaa cooldown-arvon objektina, jossa on `timestamp` ja `pnl_pct`, mutta volatiliteettipolku vähensi aiemmin koko objektia kellonajasta. Kun sama volatiliteettihälytys ilmeni uudelleen, tuloksena oli `TypeError`. 
   - **Korjaus**: `core/risk_manager.py` tarkistaa nyt `isinstance(entry, dict)` ja lukee `timestamp`-kentän samalla tavalla kuin stop-loss ja take-profit -polut.
   - **Testi**: `test_volatility_swing_cooldown_dict_format_no_type_error` ([test_risk_manager.py](../tests/test_risk_manager.py#L215-L230)).

2. **[KORJATTU] Korkea: omistuksen CLI-päivitys poistaa HODL-suojauksen.** `--set-holding` kutsui setter-metodia, joka korvasi koko omistustietueen säilyttämättä aiempia metatietoja. Testissä `hodl=True` ja 25 % tavoitepaino katosivat; HODL-suojaus palautui oletuksena pois käytöstä.
   - **Korjaus**: `core/portfolio.py`:n `set_holding()` ottaa nyt `target_weight: Optional[float] = None`, päivittää vain määrän ja hinnan, sekä säilyttää olemassa olevat metatiedot (`hodl`, `name`, aiempi `target_weight`).
   - **Testi**: `test_set_holding_preserves_hodl_and_metadata` ([test_portfolio.py](../tests/test_portfolio.py#L125-L143)).

3. **[KORJATTU] Keskitaso: SELL-suositus voi tuottaa myynnin ostosuunnan tavoite-erosta.** Myyntihaara käytti `abs(diff_val)` myös silloin, kun nykyinen omistus oli alle tavoitepainon (`diff_val > 0`). Esimerkissä 1 000 EUR omistus, 2 000 EUR tavoite ja `SELL`-suositus tuotti 1 000 EUR myyntiehdotuksen.
   - **Korjaus**: `core/rebalancer.py`:n myyntilogiikka tarkistaa nyt, että kevennysmyynti (`SELL`) toteutetaan vain kun `diff_val < 0` (nykyinen positio ylittää tavoitteen). Jos omistus on alle tavoitteen, myyntiä ei luoda. `STRONG_SELL` likvidoi koko position (`target_weight = 0.0`).
   - **Testit**: `test_sell_recommendation_does_not_sell_when_below_target_weight` ja `test_sell_recommendation_trims_excess_when_above_target_weight` ([test_rebalancer.py](../tests/test_rebalancer.py#L148-L200)).

4. **[KORJATTU] Keskitaso: sääntömoottori sivuuttaa portfolio-kohtaisen sijoitusstrategian.** `evaluate_equities` ei välittänyt `current_portfolio['global_strategy']`-arvoa fallback-arvioijalle, vaikka OpenAI-polku käytti samaa strategiatietoa. `LONG_TERM`-portfolion testiesimerkki arvioitui globaalilla `HYBRID`-asetuksella `STRONG_SELL`-suositukseksi, vaikka eksplisiittinen LONG_TERM-arvio oli `SELL`.
   - **Korjaus**: `core/ai_advisor.py`:n `evaluate_equities()` välittää nyt salkun `global_strategy`-arvon `_rule_based_tiuku_eval(stock, global_strategy=global_strat)` -kutsulle.
   - **Testi**: `test_evaluate_equities_respects_portfolio_global_strategy` ([test_ai_advisor.py](../tests/test_ai_advisor.py#L156-L172)).

5. **[KORJATTU] Keskitaso: USD-osakkeiden hinta ja tekniset hintaindikaattorit palautuvat eri valuutoissa.** Hintasarjan SMA-, EMA- ja Bollinger-arvot laskettiin dollareina ennen nykyhinnan muuntamista euroiksi, jolloin trendiluokittelu vertasi EUR-hintaa USD-keskiarvoihin.
   - **Korjaus**: `clients/market_data_client.py`: valuuttamuunnos euroiksi tehdään nyt `closes`-hintasarjalle ja `current_price`-hinnalle ennen teknisten indikaattorien laskentaa, jolloin SMA, EMA, Bollinger ja hinta ovat samassa EUR-valuutassa.
   - **Testi**: `test_market_data_client_converts_usd_indicators_consistently` ([test_etf_watchlist.py](../tests/test_etf_watchlist.py#L58-L94)).

6. **[KORJATTU] Korkea: CSV-tuonnin toisen rivin otsake voi tyhjentää kaikki ei-HODL-omistukset.** `read_file_lines` hyväksyi otsakkeen kahdelta ensimmäiseltä riviltä, mutta `import_csv` käytti aina ensimmäistä riviä DictReader-otsakkeena. Jos tiedostossa oli metatietorivi ennen sarakkeita, tuonti jäi tyhjäksi ja pyyhki ei-HODL-omistukset.
   - **Korjaus**: `utils/csv_importer.py`:n `import_csv()` etsii nyt otsikkorivin dynaamisesti ensimmäisten rivien joukosta tunnettujen avainsanojen perusteella ennen DictReaderin alustusta.
   - **Testi**: `test_import_csv_header_on_second_line_preserves_holdings` ([test_csv_importer.py](../tests/test_csv_importer.py#L220-L230)).

7. **[KORJATTU] Keskitaso: täysin tasainen kurssisarja saa RSI-arvon 0 ja ostosuosituksen.** Nollanousu ja nollalasku tuottivat epsilon-jakajan vuoksi RSI:n 0, jolloin sääntömoottori tulkitsi tilanteen ylimyydyksi ja nosti pisteet BUY-rajaan.
   - **Korjaus**: `utils/indicators.py`:n `calculate_rsi()` palauttaa neutraalin 50.0, kun `avg_gain == 0.0` ja `avg_loss == 0.0`, yhdenmukaistaen sen monisalkkudaemonin RSI-toteutuksen kanssa.
   - **Testi**: `test_flat_prices_return_neutral` ([test_indicators.py](../tests/test_indicators.py#L73-L77)).

8. **[KORJATTU] Keskitaso: `PortfolioManager`-kohtainen catastrophic stop -asetus ei vaikuta exitiin.** `PortfolioManager` tallensi position stop-hinnan konstruktorin `catastrophic_stop_pct`-asetuksen mukaisesti, mutta `evaluate_position()` käytti kiinteää 50 % vakiota position tallennetun stop-hinnan sijaan.
   - **Korjaus**: `screener/portfolio_manager.py`:n `evaluate_position()` tarkistaa ensin position oman `catastrophic_stop`- tai `trailing_stop`-arvon, ja käyttää moduulivakiota vain puuttuessa.
   - **Testi**: `test_evaluate_position_respects_custom_catastrophic_stop` ([test_portfolio_manager.py](../tests/test_portfolio_manager.py#L69-L81)).

9. **[KORJATTU] Keskitaso: vanhentunut P12 momentum-cache voi synnyttää oston uudella hinnalla eri signaalista.** Jos live-hinta saatiin mutta historiadata puuttui, vanha momentum-cache säilyi ja P12 saattoi käyttää vanhoja momentum-signaaleja uuden spot-hinnan yhteydessä.
   - **Korjaus**: `main_controller.py`:n `_fetch_live_price()` mitätöi nyt vanhentuneen momentum-välimuistin (`self.momentum_cache.pop(ticker, None)`), jos kynttilähistoriaa ei saada.
   - **Testi**: `test_fetch_live_price_invalidates_stale_momentum_cache` ([test_multi_portfolio.py](../tests/test_multi_portfolio.py#L612-L635)).

10. **[KORJATTU] Korkea: P12:n limit-hinta voi tulla edellisen tickerin momentum-datasta.** Breakout-ehdokkaiden toteutussilmukka käytti keräyssilmukan jättämää `mom`-muuttujaa hakematta ehdokkaan omia momentum-tietoja.
    - **Korjaus**: `main_controller.py`: ehdokastietueeseen tallennetaan sen oma `mom`-data (`cand["mom"]`), jota käytetään suoraan toteutussilmukassa `cand_mom`-muuttujan kautta.
    - **Testi**: `test_p12_candidate_limit_uses_own_momentum_data` ([test_action_plan_improvements.py](../tests/test_action_plan_improvements.py#L302-L364)).

11. **[KORJATTU] Korkea: `price_fetcher.py` ja `portfolio_manager.py` NaN-hinta ja puuttuva math-import.**
    - **Ongelma**: `get_realtime_data()` ja `execute_trade_signal()` eivät suodattaneet NaN-hintaa pois (`float('nan') <= 0` palauttaa `False`), jolloin NaN-hinnat tallentuivat positioiksi. Aiempi korjausyritys heitti `NameError: math is not defined`.
    - **Korjaus**: Tuotu `math`-moduuli `portfolio_manager.py`:hin ja `price_fetcher.py`:hin. Lisätty tarkistus `(isinstance(price, float) and math.isnan(price))` ja varmistettu, että NaN-hinta hylätään aina sekä hintahaussa että positioiden avaamisessa ja päivityksessä.
    - **Testi**: `test_execute_trade_signal_rejects_nan_price` ([test_portfolio_manager.py](../tests/test_portfolio_manager.py#L353-L382)).

12. **[KORJATTU] Keskitaso: Profile B hyväksyi liikevaihdon laskun, kun OCF puuttui, ja evaluate_profiles piilotti hylkäysperusteet.**
    - **Ongelma**: Anti-shrinking-sääntö vaatii shrinking-tilanteessa näyttöä positiivisesta kassavirrasta (`ocf > 0`). Lisäksi kun yhtiö hylättiin molemmista profiileista, `evaluate_profiles` palautti vain `reasons_a`.
    - **Korjaus**: `financial_metrics_engine.py`:ssä shrinking-tilassa vaaditaan `ocf is not None and ocf > 0`. `evaluate_profiles` yhdistää `reasons_a + reasons_b` silloin kun kumpikaan profiili ei täyty. Testi korjattu vastaamaan nykyistä `evaluate_profiles(hard_facts) -> dict` -sopimusta.
    - **Testi**: `test_profile_b_anti_shrinking_rejects_missing_or_negative_ocf` ([test_financial_metrics_engine.py](../tests/test_financial_metrics_engine.py#L130-L165)).

13. **[KORJATTU] Keskitaso: Puuttuva perusteluteksti kaatoi OpenRouter-core-analyysin fallbackin.**
    - **Ongelma**: `generate_core_fallback_rationale()` nosti `TypeError`in tuntemattomista keyword-parametreista, joita `nlp_analyzer.py` välitti rivillä 788.
    - **Korjaus**: Lisätty `**kwargs: Any` funktion signatuuriin `screener/nlp_analyzer.py`:ssä.
    - **Testi**: `test_generate_core_fallback_rationale_extra_kwargs_no_type_error` ([test_nlp_openrouter.py](../tests/test_nlp_openrouter.py#L234-L260)).

14. **[KORJATTU] Keskitaso: Historiahaun poikkeus jätti P12:n vanhan momentum-cachen voimaan.**
    - **Ongelma**: `_fetch_live_price()` ei tyhjentänyt `momentum_cache`a poikkeuksen sattuessa.
    - **Korjaus**: Lisätty `self.momentum_cache.pop(ticker, None)` `except Exception`-lohkoon `main_controller.py`:ssä.
    - **Testi**: `test_fetch_live_price_invalidates_cache_on_exception` ([test_multi_portfolio.py](../tests/test_multi_portfolio.py#L637-L655)).

15. **[KORJATTU] Keskitaso: Puuttuva markkina-arvo korvattiin keinotekoisesti kaavalla ADV * 100.**
    - **Ongelma**: Puuttuva markkina-arvo korvattiin arviolla `adv_local * 100`, mikä saattoi kiertää micro-cap -rajan ($300M).
    - **Korjaus**: `dynamic_universe_updater.py` hylkää kandidaatin suoraan (`return None`), jos markkina-arvoa ei pystytä luotettavasti todentamaan.
    - **Testi**: `test_validate_candidate_ticker_rejects_missing_market_cap` ([test_dynamic_universe_updater.py](../tests/test_dynamic_universe_updater.py#L151-L172)).

16. **[KORJATTU] Keskitaso: Position sizing voi ylittää ilmoitetun allokaation kalliilla osakkeilla.**
    - **Ongelma**: `shares_to_buy = max(1, int(alloc_eur / entry_price))` pakotti yhden osakkeen oston, vaikka osakkeen hinta ylitti koko kohdistetun allokaation, ja tallensi epätoden `PositionValue=alloc_eur`.
    - **Korjaus**: `screener/main_controller.py`:ssä tarkistetaan `shares_to_buy < 1` ja kandidaatti hylätään varoituksella. Lisäksi `position_value` tallennetaan todellisena toteutuneena altistuksena.
    - **Testi**: `test_screener_sizing_fx_conversion_and_expensive_stock_guard` ([test_screener_pipeline.py](../tests/test_screener_pipeline.py#L448-L536)).

17. **[KORJATTU] Korkea: Yleinen seulontahaara (P1–P10) ohitti markkinaregiimin käteispuskurin useamman oston tilanteessa.**
    - **Ongelma**: `main_controller.py`:ssä P1–P10 seulontasäännöt käyttivät `portfolio.cash_balance` sen sijaan että olisivat käyttäneet `available_cash` ja vähentäneet siitä oston jälkeen, jolloin useamman oston kierroksella käteisvaraus syötiin.
    - **Korjaus**: `main_controller.py`:ssä käytetään `available_cash` ja vähennetään `available_cash -= total_cost_acc`.
    - **Testi**: `test_general_screening_respects_regime_buffer_across_multiple_buys` ([test_action_plan_improvements.py](../tests/test_action_plan_improvements.py#L366-L408)).

18. **[KORJATTU] Korkea: Nordic-hintojen valuuttaa ei muunnettu sizingissa tai exit-PnL:ssä.**
    - **Ongelma**: `screener/main_controller.py` jakoi euroallokaation suoraan paikallisvaluutan hinnalla (SEK, DKK, NOK), ja `screener/exit_manager.py` tallensi paikallisvaluutan PnL:n suoraan kenttään `GrossPnLEur`.
    - **Korjaus**: Tuotu FX-muuntimet, muunnetaan allokaatio paikallisvaluuttaan ennen osakemäärän laskentaa, tallennetaan positioon `Currency`, ja `ExitManager` muuntaa PnL:n euroiksi käyttäen ajantasaista FX-kerrointa.
    - **Testit**: `test_nordic_exit_pnl_converts_to_eur` ([test_exit_manager.py](../tests/test_exit_manager.py#L287-L325)) ja `test_screener_sizing_fx_conversion_and_expensive_stock_guard` ([test_screener_pipeline.py](../tests/test_screener_pipeline.py#L448-L536)).

19. **[KORJATTU] Keskitaso: `main_controller.py`:n `execute_phase1_exits` pudotti `highest_price_seen`- ja `catastrophic_stop`-tiedot ennen `evaluate_position`-kutsua.**
    - **Ongelma**: Position-sanakirjassa välitettiin vain `buy_price`, `ticker` ja `buy_date`. `screener/portfolio_manager.py`:n dual confirmation -sääntö ei koskaan lauennut ja position oma stop-loss sivuutettiin.
    - **Korjaus**: Välitetään `highest_price_seen=cur_peak` ja `catastrophic_stop=pos.get("catastrophic_stop") or pos.get("trailing_stop")`.
    - **Testi**: `test_execute_phase1_exits_forwards_peak_and_catastrophic_stop` ([test_multi_portfolio.py](../tests/test_multi_portfolio.py#L685-L730)).

20. **[KORJATTU] Keskitaso: `PortfolioManager.open_position` pudotti `current_price`- ja `currency`-argumentit.**
    - **Ongelma**: `open_position()` otti argumentit vastaan mutta kutsui `execute_trade_signal` ilman niitä, pakottaen redundantit verkkohaut.
    - **Korjaus**: `execute_trade_signal()` ottaa vastaan `current_price` ja `currency` ja käyttää niitä suoraan annettaessa.
    - **Testi**: `test_open_position_passes_explicit_price_and_currency` ([test_portfolio_manager.py](../tests/test_portfolio_manager.py#L384-L408)).

## Tarkistukset

- `pytest -q tests/test_risk_manager.py tests/test_portfolio.py tests/test_rebalancer.py tests/test_ai_advisor.py tests/test_etf_watchlist.py tests/test_csv_importer.py tests/test_indicators.py`: **132 passed**.
- `pytest -q tests/test_financial_metrics_engine.py tests/test_nlp_openrouter.py tests/test_portfolio_manager.py tests/test_dynamic_universe_updater.py tests/test_multi_portfolio.py tests/test_action_plan_improvements.py tests/test_screener_pipeline.py tests/test_exit_manager.py`: **77 passed**.
- Kaikki 20 raportoitua logiikka- ja reunaehtobugia toistettu, korjattu ja varmistettu uusilla kohdennetuilla yksikkötesteillä.

## Status

Kaikki katselmoinnissa havaitut peruslogiikan virheet ja puutteet (löydökset 1–20) on korjattu ja varmennettu automaattisilla testeillä. Sovellus ja paperitestausympäristö ovat vakaassa tilassa.
