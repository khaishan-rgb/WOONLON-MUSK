"""Engine F - portfolio risk. Can veto a trade even when every other engine is bullish.

Looks at the user's REAL exposure (manual positions + positions synced from Moomoo; paper trades are kept
separate and checked against the paper account instead) and answers: if I buy one more contract of this,
is the account still sensibly diversified and is there cash to pay for it?
"""
import datetime as dt
import math

import numpy as np

import db

SEVERE = "SEVERE"


def _sector(ticker):
    try:
        from providers import providers
        info, _ = providers().info.info(ticker)
        return (info or {}).get("sector") or ("ETF" if (info or {}).get("quoteType") == "ETF" else "Unknown")
    except Exception:
        return "Unknown"


def _returns(ticker):
    try:
        from providers import providers
        h, _ = providers().stock.history(ticker)
        c = h["Close"].dropna().tail(130)
        return np.log(c / c.shift(1)).dropna()
    except Exception:
        return None


def account_value(paper=False):
    """Total account value used as the base for every percentage limit."""
    s = db.get_settings()
    if paper:
        from services import demo_equity
        return demo_equity()[0], "SENTRY Paper account equity"
    from moomoo_store import latest_funds
    funds, ts = latest_funds()
    if funds and funds.get("total_assets"):
        return float(funds["total_assets"]), f"Moomoo total assets ({ts})"
    from services import position_rows
    val = sum((r["eval"].get("value") or r["qty"] * r["entry_premium"] * 100) for a in ("real", "moomoo")
              for r in position_rows(a))
    return float(s["real_cash"]) + val, "Cash in Settings + marked option values"


def exposures(paper=False):
    accts = ("demo",) if paper else ("real", "moomoo")
    out = []
    for a in accts:
        for p in db.all("SELECT * FROM positions WHERE account=? AND status='OPEN'", (a,)):
            ev = db.jload(p["last_eval"], {})
            mark = ev.get("mark") or p["entry_premium"]
            out.append(dict(ticker=p["ticker"], value=float(mark) * float(p["qty"]) * 100, account=a))
    return out


def check(p, contracts=1, paper=False, deep=True):
    """p = opportunity payload (analysis.payload). Returns checks, ok flag and the max safe contract count."""
    s = db.get_settings()
    c = p["contract"]
    cost = float(c["ask"]) * 100 * contracts
    total, base_src = account_value(paper)
    expo = exposures(paper)
    checks = []

    def add(name, passed, detail, severity=SEVERE):
        checks.append(dict(name=name, passed=bool(passed), detail=detail, severity=severity))

    if total <= 0:
        add("Account value known", False, "Set your cash in Settings or connect Moomoo, so position size can be checked")
        return dict(ok=False, checks=checks, max_contracts=0, account_value=total, base=base_src)

    same = sum(e["value"] for e in expo if e["ticker"] == p["ticker"])
    lim_t = s["max_ticker_risk_pct"] * total
    add(f"One stock ≤ {s['max_ticker_risk_pct']:.0%} of account", same + cost <= lim_t,
        f"{p['ticker']} exposure ${same:,.0f} + ${cost:,.0f} vs limit ${lim_t:,.0f}")

    all_opt = sum(e["value"] for e in expo)
    lim_all = s["max_total_options_pct"] * total
    add(f"All options ≤ {s['max_total_options_pct']:.0%} of account", all_opt + cost <= lim_all,
        f"${all_opt:,.0f} + ${cost:,.0f} vs limit ${lim_all:,.0f}")

    if deep:
        sec = _sector(p["ticker"])
        sec_val = sum(e["value"] for e in expo if _sector(e["ticker"]) == sec) if sec != "Unknown" else 0.0
        lim_s = s["max_sector_risk_pct"] * total
        add(f"One sector ≤ {s['max_sector_risk_pct']:.0%} of account", sec == "Unknown" or sec_val + cost <= lim_s,
            f"{sec}: ${sec_val:,.0f} + ${cost:,.0f} vs limit ${lim_s:,.0f}" if sec != "Unknown" else "sector unknown",
            SEVERE if sec != "Unknown" else "INFO")
        r0 = _returns(p["ticker"])
        worst = None
        if r0 is not None and len(r0) > 40:
            for t in {e["ticker"] for e in expo if e["ticker"] != p["ticker"]}:
                r1 = _returns(t)
                if r1 is None:
                    continue
                j = r0.to_frame("a").join(r1.to_frame("b"), how="inner")
                if len(j) > 40:
                    cor = float(j["a"].corr(j["b"]))
                    if worst is None or cor > worst[1]:
                        worst = (t, cor)
        if worst and worst[1] > 0.8:
            grp = sum(e["value"] for e in expo if e["ticker"] in (worst[0], p["ticker"]))
            add("Not doubling up on a highly correlated stock", grp + cost <= lim_t * 1.5,
                f"moves with {worst[0]} (6-month correlation {worst[1]:.2f}); combined ${grp + cost:,.0f}", "WARN")
        elif worst:
            add("Correlation with holdings", True, f"highest: {worst[0]} {worst[1]:.2f}", "INFO")

    if not paper:
        from moomoo_store import latest_funds
        funds, ts = latest_funds()
        if funds and funds.get("power") is not None:
            add("Buying power (Moomoo)", float(funds["power"]) >= cost,
                f"${float(funds['power']):,.0f} available vs ${cost:,.0f} needed")
        else:
            add("Buying power", True, "unknown - Moomoo not connected; check in the app before ordering", "INFO")

    ed = p.get("earnings_date")
    te = p["plan"].get("time_exit")
    if ed and te and ed <= te:
        add("Earnings before planned exit", True, f"earnings {ed}: expect an IV drop after the report", "WARN")
    if c.get("dte") is not None and c["dte"] < 30:
        add("Enough time to expiry", False, f"only {c['dte']} days - time decay is steep", "WARN")
    if (c.get("spread_pct") or 0) > 0.08:
        add("Tight bid/ask", False, f"spread {c['spread_pct']:.1%} - you lose that just to get in and out", "WARN")

    hard_ok = all(x["passed"] for x in checks if x["severity"] == SEVERE)
    room = min(lim_t - same, lim_all - all_opt)
    max_c = max(0, int(math.floor(room / (float(c["ask"]) * 100)))) if c["ask"] else 0
    return dict(ok=hard_ok, checks=checks, max_contracts=max_c, account_value=total, base=base_src,
                ts=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
