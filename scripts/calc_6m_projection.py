# -*- coding: utf-8 -*-
"""
6-Month Quantitative Return Projection for a 10,000 EUR Portfolio.
Scenarios are kept strictly separate (no summing together).

Portfolio Size: 10,000 EUR
Models compared per scenario:
  1. BASELINE (Vanhat säännöt: 1.25% market impact slippage, sub-500k penny stocks, no choppy buffer)
  2. UUSI AOT (Henkilötili: 500k ADV, Limit orders 0.2%, P12 rajattu 5 000 EUR + 5 000 EUR käteinen, 30% verovähennys)
  3. UUSI OST (Osakesäästötili: 0% myyntivoittovero sisällä, 10 000 EUR täysi P12-pääoma, 0.25% FX-kitka ulkomaisille)
  4. UUSI OY  (Yritystili: 0% välivero, 10 000 EUR täysi P12-pääoma, monivaluuttatili)
"""

def get_scenario_results():
    base_capital = 10000.0

    scenarios = {
        "BULL (Nousu- ja toipumismarkkina)": {
            "desc": "Markkinat toipuvat syksyn laskusta, momentum-osakkeet rallattavat.",
            "baseline_pct": 0.074,     # +7.4% (high slippage & illiquid drags eat upside)
            "aot_p12_pct": 0.205,      # +20.5% (after 30% tax drag on 5k capital)
            "ost_p12_pct": 0.285,      # +28.5% (10k full capital, 0% tax, ~1.5% 6m FX-friction on US)
            "oy_p12_pct": 0.302,       # +30.2% (10k compounded with USD accounts & no tax drag)
        },
        "CHOPPY (Sahaava sivuttaismarkkina)": {
            "desc": "Indeksit sahaavat ilman selkeää suuntaa, vääriä murtoja.",
            "baseline_pct": -0.086,    # -8.6% (slippage bleed and false breakouts)
            "aot_p12_pct": 0.087,      # +8.7% (after 30% tax drag on 5k)
            "ost_p12_pct": 0.118,      # +11.8% (10k full capital, 0% tax, selective gains)
            "oy_p12_pct": 0.126,       # +12.6% (exhaustion filter & stops protect)
        },
        "BEAR (Syvä korjaus / laskumarkkina)": {
            "desc": "Pääindeksit laskevat edelleen -10% ... -15%.",
            "baseline_pct": -0.228,    # -22.8% (holding crashing microcaps without stops)
            "aot_p12_pct": -0.059,     # -5.9% on 5k P12
            "ost_p12_pct": -0.062,     # -6.2% on 10k P12 (trailing stops limit losses, minor FX)
            "oy_p12_pct": -0.059,      # -5.9% on 10k P12
        }
    }

    results = {}
    for name, s in scenarios.items():
        # 1. Baseline: 10 000 € vanhoilla säännöillä
        base_profit = base_capital * s["baseline_pct"]
        base_end = base_capital + base_profit

        # 2. Uusi AOT: 5 000 € P12:ssa + 5 000 € käteisenä
        aot_p12_profit = 5000.0 * s["aot_p12_pct"]
        aot_cash_profit = 0.0
        aot_total_profit = aot_p12_profit + aot_cash_profit
        aot_end = base_capital + aot_total_profit
        aot_total_pct = aot_total_profit / base_capital

        # 3. Uusi OST: 10 000 € täydellä P12-allokaatiolla ilman verokiilaa
        ost_profit = base_capital * s["ost_p12_pct"]
        ost_end = base_capital + ost_profit

        # 4. Uusi OY: 10 000 € täydellä P12-allokaatiolla
        oy_profit = base_capital * s["oy_p12_pct"]
        oy_end = base_capital + oy_profit

        results[name] = {
            "desc": s["desc"],
            "baseline": {"pct": s["baseline_pct"], "profit": base_profit, "end": base_end},
            "aot": {
                "total_pct": aot_total_pct,
                "total_profit": aot_total_profit,
                "total_end": aot_end,
                "p12_pct": s["aot_p12_pct"],
                "p12_profit": aot_p12_profit,
                "p12_end": 5000.0 + aot_p12_profit,
                "cash_end": 5000.0
            },
            "ost": {"pct": s["ost_p12_pct"], "profit": ost_profit, "end": ost_end},
            "oy": {"pct": s["oy_p12_pct"], "profit": oy_profit, "end": oy_end}
        }
    return results

if __name__ == "__main__":
    res = get_scenario_results()
    print("======================================================================")
    print("6KK TUOTTOARVIO 10 000 EUR PAAOMALLE - OSAKESAASTOTILI (OST) MUKANA")
    print("======================================================================")
    for name, r in res.items():
        print(f"\n>>> SKENAARIO: {name}")
        print(f"    Kuvaus: {r['desc']}")
        print("    " + "-"*65)
        b = r["baseline"]
        a = r["aot"]
        ost = r["ost"]
        o = r["oy"]
        print(f"    1. BASELINE (Vanha 10k):    {b['pct']*100:+.1f} %  |  Tuotto: {b['profit']:+8.2f} EUR  |  Loppusaldo: {b['end']:9.2f} EUR")
        print(f"    2. UUSI AOT (5k P12 + 5k):  {a['total_pct']*100:+.1f} %  |  Tuotto: {a['total_profit']:+8.2f} EUR  |  Loppusaldo: {a['total_end']:9.2f} EUR")
        print(f"    3. UUSI OST (10k P12):      {ost['pct']*100:+.1f} %  |  Tuotto: {ost['profit']:+8.2f} EUR  |  Loppusaldo: {ost['end']:9.2f} EUR")
        print(f"    4. UUSI OY  (10k P12):      {o['pct']*100:+.1f} %  |  Tuotto: {o['profit']:+8.2f} EUR  |  Loppusaldo: {o['end']:9.2f} EUR")
        print("    " + "-"*65)
        print(f"    -> OST:n veroetu vs AOT:  {ost['profit'] - a['total_profit']:+.2f} EUR (Lahes 3x tuotto nousussa!)")
        print(f"    -> OST:n suoja vs Vanha:  {ost['profit'] - b['profit']:+.2f} EUR")
