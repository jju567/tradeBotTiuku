# Peruslogiikan koodikatselmointi

## Edistyminen

- Osa 1 valmis: salkun riskitarkistukset, tasapainotusehdotukset, portfolio-tila, AI-arviointi ja viikkajonon/markkinamonitorin keskeiset kutsupolut.
- Osa 2 valmis: markkinadatan valuuttayksiköt, indikaattorien rajatapaukset ja CSV-tuonnin otsakekäsittely.
- Osa 3 valmis: screener-salkun exit-säännöt sekä monisalkkudaemonin P12 momentum-ostot ja välimuistinhallinta.
- Kaikki löydökset 1–10 on korjattu ja varmennettu kattavilla yksikkötesteillä.
- Katselmointi ei ole vielä koko sovelluksen kattava.
- Seuraava osa: jatka monisalkkudaemonin muiden positionhallintapolkujen ja lisämoduulien (kuten `screener/`-liiketoimintalogiikan) katselmointia.

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

## Tarkistukset

- `pytest -q tests/test_risk_manager.py tests/test_portfolio.py tests/test_rebalancer.py tests/test_ai_advisor.py`: **80 passed**.
- `pytest -s tests/test_etf_watchlist.py`: **3 passed**.
- `pytest -s tests/test_csv_importer.py`: **31 passed**.
- `pytest -s tests/test_indicators.py`: **23 passed**.
- `pytest -s tests/test_portfolio_manager.py`: **12 passed**.
- `pytest -s tests/test_multi_portfolio.py -k test_fetch_live_price_invalidates_stale_momentum_cache`: **1 passed**.
- `pytest -s tests/test_action_plan_improvements.py -k test_p12_candidate_limit_uses_own_momentum_data`: **1 passed**.
- Kaikki 10 raportoitua logiikka- ja reunaehtobugia toistettu ja varmistettu korjatuiksi uusilla kohdennetuilla yksikkötesteillä.

## Jatkaminen

Jatka tarkastelemalla `main_controller.py`-tiedoston positionhallinnan ja market-data-syklin muita ehtoja sekä niihin liittyviä testejä. Sen jälkeen jatka jäljellä oleviin `screener/`-liiketoimintalogiikan moduuleihin.
