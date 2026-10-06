"""Output (spec 21, 22): simple Layman screen first, details underneath."""
import math
import config

ICON = {"BUY": "🟢 BUY", "WATCH": "🟡 HOLD-WATCH", "AVOID": "🔴 SELL-AVOID"}


def pct(x, sign=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:+.0%}" if sign else f"{x:.0%}"


def money(x):
    return f"${x:,.2f}"


def contract_name(t):
    legs = t["legs"]
    k = legs[0]["kind"].upper()
    if len(legs) == 2:
        return f"{t['ticker']} {t['expiry']} {legs[0]['K']:g}/{legs[1]['K']:g} {k} spread"
    return f"{t['ticker']} {t['expiry']} {legs[0]['K']:g} {k}"


def layman(res):
    t, st, ex = res["trade"], res["stats"], res["exit"]
    n = res["contracts_affordable"]
    afford = (f"{n} contract(s) (${n * t['cost'] * 100:,.0f}); rest stays in cash" if n > 0 else
              f"0 - one contract costs ${t['cost'] * 100:,.0f}, more than $1,000")
    why = []
    for _, txt in sorted(res["committee"]["bull"], reverse=True)[:3]:
        why.append(txt + ".")
    if not why:
        why = ["No strong supporting evidence."]
    risks = [txt for _, txt in sorted(res["committee"]["bear"], reverse=True)[:3]] or ["Losing the full premium."]
    strikes = " / ".join("%g" % l["K"] for l in t["legs"])
    L = [
        f"{ICON[res['action']]}   ({res['direction']}, grade {res['grade']})",
        "",
        f"Stock: {t['ticker']}   Current Price: {money(res['price'])}  [{res['price_time']}]",
        f"Option: {t['strategy']} ({t['moneyness']})",
        f"Strike: {strikes}   Expiry: {t['expiry']} ({t['dte']} days)",
        f"Premium: {money(t['cost'])} per share = {money(t['cost'] * 100)} per contract (paying the ask)",
        f"If investing $1,000: {afford}",
        "",
        "WHY?", *[f"  {w}" for w in why],
        "",
        "AI EXPECTATION (stock price on " + t["exit_date"] + ")",
        f"  Bear Case: {money(st['stock_q10'])}   Base Case: {money(st['stock_q50'])}   Bull Case: {money(st['stock_q90'])}",
        "",
        "$1,000 SIMULATION (following the sell rules below)",
        f"  Bad scenario (1 in 10):  ${1000 * (1 + st['q10']):,.0f}",
        f"  Likely scenario (median): ${1000 * (1 + st['median']):,.0f}",
        f"  Good scenario (1 in 4):  ${1000 * (1 + st['q75']):,.0f}",
        f"  Exceptional (1 in 20):   ${1000 * (1 + st['q95']):,.0f}",
        "",
        "PROBABILITIES",
        f"  Chance of Profit: {pct(st['p_profit'])} following the sell rules "
        f"(no rules: {pct(st['p_profit_no_rules'])}; held to expiry: {pct(st['p_profit_expiry'])})",
        f"  Chance of 50%+: {pct(st['p_50'])}   Chance of 100%+: {pct(st['p_100'])}",
        f"  Chance of Total Loss: {pct(st['p_total_loss'])} with the loss limit, "
        f"{pct(st['p_total_loss_no_stop'])} if you ignore it",
        f"  Expected Value: {st['ev_dollars']:+,.0f} per $1,000 (model estimate, not a promise)",
        "",
        "WHAT COULD GO WRONG?", *[f"  - {x}" for x in risks],
        "",
        "WHEN DO I SELL?",
        f"  Profit target: {ex['profit']}",
        f"  Partial profit: {ex['partial']}",
        f"  Loss limit: {ex['loss']}",
        f"  Time exit: {ex['time']}",
        f"  Thesis invalidation: {ex['thesis']}",
    ]
    if ex.get("pre_earnings"):
        L.append(f"  Earnings: {ex['pre_earnings']}")
    L += ["", f"AI CONFIDENCE: {res['confidence']:.0f}/100     Data Quality: {res['dq']}",
          f"Suggested max risk: {res['risk_budget_pct']:.1f}% of your options-risk budget "
          "(never money for bills or emergencies)"]
    return "\n".join(L)


def dashboard(results):
    ok = [r for r in results if "trade" in r]
    ok.sort(key=lambda r: r["score"], reverse=True)
    L = ["Rank | Stock | Action | Best Option | Cost | Target | P(Profit) | Exp Return | Total-Loss (no stop) | Score",
         "-----|-------|--------|-------------|------|--------|-----------|------------|------------|------"]
    for i, r in enumerate(ok[:10], 1):
        t, st = r["trade"], r["stats"]
        L.append(f"{i} | {t['ticker']} | {ICON[r['action']]} | {contract_name(t)} | {money(t['cost'] * 100)} | "
                 f"{money(r['exit']['target_price'] * 100)} | {pct(st['p_profit'])} | {pct(st['mean'], True)} | "
                 f"{pct(st['p_total_loss_no_stop'])} | {r['score']:.0f}")
    return "\n".join(L)


def detail(r):
    t, st = r["trade"], r["stats"]
    L = [f"### {t['ticker']} {r['direction']} - {ICON[r['action']]} - score {r['score']:.0f} ({r['grade']})", ""]
    L.append("Engine scores (direction-adjusted): " + ", ".join(f"{k} {v:.0f}" for k, v in r["components"].items()))
    if r["penalties"]:
        L.append("Penalties: " + ", ".join(f"{n} -{p}" for n, p in r["penalties"]))
    L.append(f"Macro: {r['macro']['label']} ({r['macro']['score']:.0f}) - " + "; ".join(r["macro"]["notes"]))
    L.append("Fundamentals: " + "; ".join(r["fund"]["notes"]))
    L.append("Technicals: " + "; ".join(r["tech"]["notes"]) + f"; RSI {r['tech']['rsi']:.0f}")
    L.append("Expectations: " + "; ".join(r["exp"]["notes"]))
    for c in r["catalysts"]["catalysts"]:
        L.append(f"Catalyst: {c['event']} ({c['date']}) impact {c['impact']}; priced in? {c['priced_in']}")
    L.append(f"Contract: delta {t['delta']:+.2f}, gamma {t['gamma']:.4f}, theta {money(t['theta'])}/day, "
             f"vega {money(t['vega'])}/IV pt, IV {pct(t['iv'])}, break-even {money(t['breakeven'])}, "
             f"max loss {money(t['max_loss'])}" + (f", max gain {money(t['max_gain'])}" if t["max_gain"] else ""))
    L.append(f"Fair value (BS + binomial on realised-vol forecast {pct(r['rv_fair'])}): {money(t['fair'])} vs market "
             f"mid -> mispricing {pct(t['mispricing'], True)} (model estimate)")
    L.append(f"Simulation: {st['n_paths']:,} pooled paths across {len(config.REGIMES)} regimes, vol {pct(r['sigma_sim'])}; "
             f"P(+25%) {pct(st['p_25'])}, P(+200%) {pct(st['p_200'])}, P(-50%) {pct(st['p_loss50'])}")
    L.append("")
    L.append("Why this contract beat nearby alternatives (ranked by probability-adjusted return):")
    for a in [t] + r["alternatives"]:
        s = a["stats"]
        L.append(f"  {contract_name(a)} [{a['moneyness']}] cost {money(a['cost'] * 100)}: P(profit) {pct(s['p_profit'])}, "
                 f"avg {pct(s['mean'], True)}, total-loss without stop {pct(s['p_total_loss_no_stop'])}")
    L.append("")
    L.append("Time path (median outcomes; 'flat' = value if the stock doesn't move -> pure theta/IV effect):")
    L.append("  When | Stock | Option | If flat | Delta")
    for p in r["time_path"]:
        L.append(f"  {p['when']} | {money(p['stock_median'])} | {money(p['option_median'])} | "
                 f"{money(p['flat_stock_value'])} | {p['delta']:+.2f}")
    L.append("")
    L.append(f"$1,000 profit simulator at {t['exit_date']} (IV + time decay included, exit at bid):")
    for p in r["profit"]:
        L.append(f"  Stock {p['move']:+.0%} -> {money(p['stock'])}: option ~{money(p['option'])}, "
                 f"$1,000 -> ${p['value']:,.0f} ({p['ret']:+.0%})")
    bt = r["backtest"]
    if bt["n"]:
        L.append(f"\nBacktest ({bt['confidence']}): {bt['n']} similar setups, win rate {pct(bt['win_rate'])}, median "
                 f"{pct(bt['median'], True)}, worst {pct(bt['worst'], True)}, profit factor {bt['profit_factor']:.2f}. {bt['note']}")
    else:
        L.append(f"\nBacktest: LOW CONFIDENCE - {bt['note']}")
    com = r["committee"]
    L.append("\nInvestment committee votes: " + ", ".join(f"{k}: {v}" for k, v in com["votes"].items()))
    if com["vetoes"]:
        L.append("Risk-manager vetoes: " + "; ".join(com["vetoes"]))
    L.append(f"Bull evidence weight {com['bull_w']:.1f} vs bear {com['bear_w']:.1f} -> contrarian check "
             + ("PASSED" if com["contrarian_pass"] else "FAILED (do not buy)"))
    for q, a in com["contrarian"].items():
        L.append(f"  {q}? {a}")
    L.append("\nData flags: " + ("; ".join(f"[{s}] {m}" for s, m in r["flags"]) or "none"))
    L.append("Sources: " + ", ".join(f"{k} @ {v}" for k, v in r["sources"].items()))
    return "\n".join(L)


def build(results, demo=False, run_time=""):
    out = []
    if demo:
        out.append("!!! DEMO MODE - SYNTHETIC DATA - NOT REAL PRICES - NOT A RECOMMENDATION !!!\n")
    out.append(f"AI GLOBAL OPTIONS OPPORTUNITY MACHINE - run {run_time}")
    out.append("Research tool output. Not financial advice. Bought options can lose 100% of the premium.\n")
    traded = [r for r in results if "trade" in r]
    buys = sorted([r for r in traded if r["action"] == "BUY"], key=lambda r: r["score"], reverse=True)
    out.append("=" * 70)
    if buys:
        top = buys[0]
        out.append("AI'S #1 TRADE / BEST TRADE TODAY\n")
        out.append(layman(top))
        out.append(f"\nWhy #1: highest final score ({top['score']:.0f}) among {len(buys)} BUY signal(s), "
                   "passed the risk manager and the contrarian check.")
    else:
        out.append("NO TRADE - CASH IS A POSITION.")
        out.append("Nothing passed every test (score >= 70, positive expected value, risk vetoes clear, "
                   "bull case stronger than bear case).")
        watch = sorted(traded, key=lambda r: r["score"], reverse=True)
        if watch:
            out.append("\nClosest candidate (for watching only):\n")
            out.append(layman(watch[0]))
    out.append("=" * 70)
    out.append("\nTOP 10 DASHBOARD\n")
    out.append(dashboard(results))
    errs = [r for r in results if "trade" not in r]
    if errs:
        out.append("\nSkipped: " + "; ".join(f"{r['ticker']} {r.get('direction', '')}: {r.get('error', '')}" for r in errs))
    out.append("\n" + "=" * 70 + "\nFULL DETAIL\n")
    for r in sorted(traded, key=lambda r: r["score"], reverse=True)[:10]:
        out.append(detail(r))
        out.append("")
    return "\n".join(out)
