# -*- coding: utf-8 -*-
"""
6-Month Quantitative Return Projection Model for TradeBotTiuku
Comparing:
  1. BASELINE (Old rules: 1.25% slippage, penny stocks, no cash reserve in choppy)
  2. NEW OY (Corporate: 500k ADV, Limit orders 0.2%, 40% cash in choppy, exhaustion filter, P12 10k compounding at 0% tax)
  3. NEW AOT (Personal AOT: 500k ADV, Limit orders 0.2%, P12 capped at 5k to minimize turnover, 30% tax drag)
"""

def simulate_6m(scenario):
    if scenario == 'Bull':
        # Recovery scenario after 3-week dip
        core_oy_ret = 0.16          # +16% core fundamental recovery
        core_aot_ret = 0.16 * 0.90  # ~14.4% (slight tax drag on partial take profits)
        p12_monthly_oy = 0.045      # +4.5% net monthly in Oy
        p12_monthly_aot = 0.045 * 0.70  # +3.15% net monthly after 30% tax
        
        core_base_ret = 0.12        # baseline missed exits / higher slippage
        p12_base_monthly = 0.012    # heavy 1.25% slippage eats breakout alpha
    elif scenario == 'Choppy':
        # Sideways choppy market
        core_oy_ret = 0.035         # +3.5% (40% cash buffer protects, partial TPs lock +25% spikes)
        core_aot_ret = 0.035 * 0.92 # ~3.2%
        p12_monthly_oy = 0.020      # +2.0% monthly in Oy
        p12_monthly_aot = 0.020 * 0.70  # +1.4% monthly
        
        core_base_ret = -0.05       # whipsaws and false breakouts
        p12_base_monthly = -0.015   # continuous slippage bleed
    else:  # Bear
        # Deep continuation of drop (-15% index)
        core_oy_ret = -0.065        # -6.5% (stops trigger at -8% US / -12% Nordic, 40% cash buffer)
        core_aot_ret = -0.065       # same
        p12_monthly_oy = -0.010     # -1.0% monthly (trailing stop limits damage)
        p12_monthly_aot = -0.010    # -1.0% monthly
        
        core_base_ret = -0.165      # full drawdown, holding penny stocks down 30-50%
        p12_base_monthly = -0.042   # severe drawdown on illiquid microcaps

    # 6-Month Compounded Rates
    p12_base_6m = ((1 + p12_base_monthly)**6 - 1)
    p12_oy_6m = ((1 + p12_monthly_oy)**6 - 1)
    p12_aot_6m = ((1 + p12_monthly_aot)**6 - 1)

    # BASELINE: Core 20k, P12 10k
    p12_base_eur = 10000 * (1 + p12_base_6m)
    core_base_eur = 20000 * (1 + core_base_ret)
    base_total = core_base_eur + p12_base_eur

    # OY: Core 20k, P12 10k
    p12_oy_eur = 10000 * (1 + p12_oy_6m)
    core_oy_eur = 20000 * (1 + core_oy_ret)
    oy_total = core_oy_eur + p12_oy_eur

    # AOT: Core 25k (20k core + 5k cash/core), P12 5k
    p12_aot_eur = 5000 * (1 + p12_aot_6m)
    core_aot_eur = 25000 * (1 + core_aot_ret)
    aot_total = core_aot_eur + p12_aot_eur

    return {
        'Baseline': (base_total - 30000) / 30000,
        'Base_EUR': base_total,
        'Base_Core_EUR': core_base_eur,
        'Base_P12_EUR': p12_base_eur,
        'OY': (oy_total - 30000) / 30000,
        'OY_EUR': oy_total,
        'OY_Core_EUR': core_oy_eur,
        'OY_P12_EUR': p12_oy_eur,
        'AOT': (aot_total - 30000) / 30000,
        'AOT_EUR': aot_total,
        'AOT_Core_EUR': core_aot_eur,
        'AOT_P12_EUR': p12_aot_eur,
        'P12_OY_6M': p12_oy_6m,
        'P12_AOT_6M': p12_aot_6m,
        'P12_Base_6M': p12_base_6m
    }

scenarios = ['Bull', 'Choppy', 'Bear']
probs = [0.50, 0.35, 0.15]
res = {s: simulate_6m(s) for s in scenarios}

for s in scenarios:
    r = res[s]
    print(f"=== SKENAARIO: {s.upper()} (Todennäköisyys: {int(probs[scenarios.index(s)]*100)} %) ===")
    print(f"  BASELINE: {r['Baseline']*100:+.2f}% ({r['Base_EUR']:,.2f} EUR)")
    print(f"  UUSI OY:  {r['OY']*100:+.2f}% ({r['OY_EUR']:,.2f} EUR) | P12 Tuotto: {r['P12_OY_6M']*100:+.1f}%")
    print(f"  UUSI AOT: {r['AOT']*100:+.2f}% ({r['AOT_EUR']:,.2f} EUR) | P12 Tuotto: {r['P12_AOT_6M']*100:+.1f}%\n")

ev_base = sum(res[s]['Baseline'] * p for s, p in zip(scenarios, probs))
ev_oy = sum(res[s]['OY'] * p for s, p in zip(scenarios, probs))
ev_aot = sum(res[s]['AOT'] * p for s, p in zip(scenarios, probs))

# Component-specific EVs
p12_aot_ev_eur = sum((res[s]['AOT_P12_EUR'] - 5000) * p for s, p in zip(scenarios, probs))
core_aot_ev_eur = sum((res[s]['AOT_Core_EUR'] - 25000) * p for s, p in zip(scenarios, probs))

p12_oy_ev_eur = sum((res[s]['OY_P12_EUR'] - 10000) * p for s, p in zip(scenarios, probs))
core_oy_ev_eur = sum((res[s]['OY_Core_EUR'] - 20000) * p for s, p in zip(scenarios, probs))

print("======================================================================")
print("6KK TUOTON ODOTUSARVOT (E[R]) JA PAAOMA (Lahtotaso: 30 000 EUR)")
print("======================================================================")
print(f"BASELINE (Vanha):   {ev_base*100:+.2f}%  ->  {30000*(1+ev_base):,.2f} EUR")
print(f"UUSI OY (Yritys):   {ev_oy*100:+.2f}%  ->  {30000*(1+ev_oy):,.2f} EUR")
print(f"  |-- Core (20 000 EUR paaoma):  +{core_oy_ev_eur/20000*100:.2f}%  (+{core_oy_ev_eur:,.2f} EUR)")
print(f"  +-- P12  (10 000 EUR paaoma):  +{p12_oy_ev_eur/10000*100:.2f}%  (+{p12_oy_ev_eur:,.2f} EUR)")
print(f"UUSI AOT (Henkilo): {ev_aot*100:+.2f}%  ->  {30000*(1+ev_aot):,.2f} EUR")
print(f"  |-- Core (25 000 EUR paaoma):  +{core_aot_ev_eur/25000*100:.2f}%  (+{core_aot_ev_eur:,.2f} EUR)")
print(f"  +-- P12  ( 5 000 EUR paaoma):  +{p12_aot_ev_eur/5000*100:.2f}%  (+{p12_aot_ev_eur:,.2f} EUR)")
print("----------------------------------------------------------------------")
print(f"Yritysmallin etu (OY vs AOT): {(ev_oy - ev_aot)*100:+.2f}%-yksikkoa (+{(ev_oy - ev_aot)*30000:,.2f} EUR)")
print(f"Arkkitehtuurin kokonaisalfa (OY vs Baseline): {(ev_oy - ev_base)*100:+.2f}%-yksikkoa (+{(ev_oy - ev_base)*30000:,.2f} EUR)")

