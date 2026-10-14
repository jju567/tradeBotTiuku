# Peruslogiikan koodikatselmointi

## Edistyminen

- Osa 1 valmis: salkun riskitarkistukset, tasapainotusehdotukset, portfolio-tila, AI-arviointi ja viikkajonon/markkinamonitorin keskeiset kutsupolut.
- Katselmointi ei ole vielä koko sovelluksen kattava.
- Seuraava osa: tarkista `clients/market_data_client.py`, `utils/indicators.py` ja `utils/csv_importer.py` yksikkötestien ja datamuotojen kanssa.
- Katselmointiin ei tehty lähdekoodimuutoksia.

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

## Tarkistukset

- `pytest -q tests/test_risk_manager.py tests/test_portfolio.py tests/test_rebalancer.py tests/test_ai_advisor.py tests/test_market_monitor.py`: **80 passed**.
- Koko järjestelmän kattava testipatteristo `pytest`: **318 passed**.
- Kaikki 4 raportoitua reunaehtobugia toistettu ja varmistettu korjatuiksi uusilla kohdennetuilla yksikkötesteillä.

## Jatkaminen

Jatka tästä `clients/market_data_client.py`- ja `utils/indicators.py`-logiikasta sekä niiden testeistä. Pidä löydökset tässä tiedostossa, merkitse käsitellyt moduulit ja siirrä jatkoraja seuraavan valmiin osan jälkeen.
