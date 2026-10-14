# -*- coding: utf-8 -*-
"""
6-Month Quantitative Return Projection for a 10,000 EUR Portfolio.
Scenarios are kept strictly separate (no summing together).

Portfolio Size: 10,000 EUR
Models compared per scenario:
  1. BASELINE (Vanhat säännöt: 1.25% market impact slippage, sub-500k penny stocks, no choppy buffer)
  2. UUSI OY (Yritystili: 500k ADV, Limit orders 0.2%, 10 000 EUR täysi P12-pääoma, 0% välivero)
  3. UUSI AOT (Henkilötili: 500k ADV, Limit orders 0.2%, P12 rajattu 5 000 EUR + 5 000 EUR käteinen, 30% verovähennys)
"""

def get_scenario_results():
    base_capital = 10000.0

    scenarios = {
        "BULL (Nousu- ja toipumismarkkina)": {
            "desc": "Markkinat toipuvat syksyn laskusta, momentum-osakkeet rallattavat.",
            "baseline_pct": 0.074,     # +7.4% (high slippage & illiquid drags eat upside)
            "oy_p12_pct": 0.302,       # +30.2% (10k compounded with limit orders & 500k ADV)
            "aot_p12_pct": 0.205,      # +20.5% (after 30% tax drag on gains)
        },
        "CHOPPY (Sahaava sivuttaismarkkina)": {
            "desc": "Indeksit sahaavat ilman selkeää suuntaa, vääriä murtoja.",
            "baseline_pct": -0.086,    # -8.6% (slippage bleed and false breakouts)
            "oy_p12_pct": 0.126,       # +12.6% (exhaustion filter & stops protect, selective gains)
            "aot_p12_pct": 0.087,      # +8.7% (after 30% tax drag)
        },
        "BEAR (Syvä korjaus / laskumarkkina)": {
            "desc": "Pääindeksit laskevat edelleen -10% ... -15%.",
            "baseline_pct": -0.228,    # -22.8% (holding crashing microcaps without stops)
            "oy_p12_pct": -0.059,      # -5.9% (trailing stops -6% and regional stops protect)
            "aot_p12_pct": -0.059,     # -5.9%
        }
    }

    results = {}
    for name, s in scenarios.items():
        # 1. Baseline: 10 000 € vanhoilla säännöillä
        base_profit = base_capital * s["baseline_pct"]
        base_end = base_capital + base_profit

        # 2. Uusi OY: 10 000 € täydellä P12-allokaatiolla
        oy_profit = base_capital * s["oy_p12_pct"]
        oy_end = base_capital + oy_profit

        # 3. Uusi AOT: 5 000 € P12:ssa + 5 000 € käteisenä
        aot_p12_profit = 5000.0 * s["aot_p12_pct"]
        aot_cash_profit = 0.0  # käteispuskuri säilyttää pääoman
        aot_total_profit = aot_p12_profit + aot_cash_profit
        aot_end = base_capital + aot_total_profit
        aot_total_pct = aot_total_profit / base_capital

        results[name] = {
            "desc": s["desc"],
            "baseline": {"pct": s["baseline_pct"], "profit": base_profit, "end": base_end},
            "oy": {"pct": s["oy_p12_pct"], "profit": oy_profit, "end": oy_end},
            "aot": {
                "total_pct": aot_total_pct,
                "total_profit": aot_total_profit,
                "total_end": aot_end,
                "p12_pct": s["aot_p12_pct"],
                "p12_profit": aot_p12_profit,
                "p12_end": 5000.0 + aot_p12_profit,
                "cash_end": 5000.0
            }
        }
    return results

if __name__ == "__main__":
    res = get_scenario_results()
    print("======================================================================")
    print("6KK TUOTTOARVIO 10 000 EUR PAAOMALLE - SKENAARIOT ERILLISINA")
    print("======================================================================")
    for name, r in res.items():
        print(f"\n>>> SKENAARIO: {name}")
        print(f"    Kuvaus: {r['desc']}")
        print("    " + "-"*62)
        b = r["baseline"]
        o = r["oy"]
        a = r["aot"]
        print(f"    1. BASELINE (Vanha 10k):    {b['pct']*100:+.1f} %  |  Tuotto: {b['profit']:+8.2f} EUR  |  Loppusaldo: {b['end']:9.2f} EUR")
        print(f"    2. UUSI OY (10k P12):       {o['pct']*100:+.1f} %  |  Tuotto: {o['profit']:+8.2f} EUR  |  Loppusaldo: {o['end']:9.2f} EUR")
        print(f"    3. UUSI AOT (5k P12 + 5k):  {a['total_pct']*100:+.1f} %  |  Tuotto: {a['total_profit']:+8.2f} EUR  |  Loppusaldo: {a['total_end']:9.2f} EUR")
        print(f"       * josta P12 (5 000 EUR): {a['p12_pct']*100:+.1f} %  |  Tuotto: {a['p12_profit']:+8.2f} EUR  |  Loppusaldo: {a['p12_end']:9.2f} EUR")
        print(f"       * josta Kateinen:        0.0 %  |  Tuotto:     0.00 EUR  |  Loppusaldo: {a['cash_end']:9.2f} EUR")
        print("    " + "-"*62)
        print(f"    -> Yritysmallin (OY) lisatuotto vs AOT: {o['profit'] - a['total_profit']:+.2f} EUR")
        print(f"    -> Uuden arkkitehtuurin suoja vs Vanha: {o['profit'] - b['profit']:+.2f} EUR")
