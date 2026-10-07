"""Options profit simulator (master spec section 8).

The user picks a contract and their own assumptions; we show what the option could be worth at several exit
dates (not only at expiry), across bullish / base / bearish / severe-downside scenarios, plus a Monte Carlo
distribution and a break-even chart. Every input is labelled OBSERVED (from a live/delayed quote) or
ASSUMPTION (typed by the user or a model default), so model assumptions are never mistaken for market data.
"""
import datetime as dt
import math

import numpy as np

import config
import db
from pricing import bs_price, implied_vol, greeks

IV_SCENARIOS = {"IV crush (-30%)": 0.7, "IV unchanged": 1.0, "IV rises (+30%)": 1.3}


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), n)


def run(ticker, kind, strike, expiry, qty=1, premium=None, iv=None, move_pct=None, hold_days=None,
        iv_scenario="IV unchanged", paths=20000, seed=config.SEED):
    from providers import load_ticker
    import services
    ticker, kind = ticker.upper().strip(), kind.lower()
    strike, qty = float(strike), max(1, int(qty))
    exp_date = dt.date.fromisoformat(expiry)
    dte = (exp_date - dt.date.today()).days
    if dte <= 0:
        raise ValueError("Expiry must be in the future")
    inputs, notes = {}, []

    td = load_ticker(ticker, expiries=[expiry], full=False)
    if td.price is None:
        raise ValueError(f"No price for {ticker}")
    S0 = float(td.price)
    inputs["Stock price"] = dict(value=_r(S0), kind="OBSERVED", source=f"{td.sources[1]['source'] if len(td.sources) > 1 else 'quote'}"
                                 f" · {td.price_time}")
    macro, _ = services.macro_ctx()
    r, q = macro["r"], td.dividend_yield
    inputs["Risk-free rate"] = dict(value=_r(r, 4), kind="OBSERVED" if "FRED" in macro["r_source"] else "ASSUMPTION",
                                    source=macro["r_source"])

    bid = ask = None
    if expiry in td.chains and td.chain_status not in ("UNAVAILABLE",):
        df = td.chains[expiry]["calls" if kind == "call" else "puts"]
        row = df[(df["strike"] - strike).abs() < 1e-6]
        if not row.empty and float(row["bid"].iloc[0] or 0) > 0 and float(row["ask"].iloc[0] or 0) > 0:
            bid, ask = float(row["bid"].iloc[0]), float(row["ask"].iloc[0])
    T = dte / 365
    if premium is None:
        if ask is None:
            raise ValueError("No current quote for this contract - enter the premium yourself to simulate")
        premium = ask
        inputs["Premium (you pay the ask)"] = dict(value=_r(ask), kind="OBSERVED", source=f"option chain · {td.chain_status}")
    else:
        premium = float(premium)
        inputs["Premium"] = dict(value=_r(premium), kind="ASSUMPTION", source="entered by you")
    if iv is None:
        mid = (bid + ask) / 2 if bid and ask else premium
        iv = implied_vol(mid, S0, strike, T, r, kind, q)
        if iv:
            inputs["Implied volatility"] = dict(value=_r(iv, 4), kind="OBSERVED" if bid else "ASSUMPTION",
                                                source="solved from the option's mid price" if bid else "solved from your premium")
        else:
            c = td.hist["Close"].dropna()
            iv = float(np.log(c / c.shift(1)).dropna().tail(60).std() * math.sqrt(252)) if td.hist is not None else 0.35
            inputs["Implied volatility"] = dict(value=_r(iv, 4), kind="ASSUMPTION", source="60-day realised volatility (no valid IV)")
    else:
        iv = float(iv)
        inputs["Implied volatility"] = dict(value=_r(iv, 4), kind="ASSUMPTION", source="entered by you")
    from providers import providers
    if providers().mode == "demo":
        for v in inputs.values():
            if v["kind"] == "OBSERVED":
                v["kind"] = "SYNTHETIC"
        notes.append("DEMO MODE: market inputs are synthetic test data.")
    ivm = IV_SCENARIOS.get(iv_scenario, 1.0)
    hold = int(hold_days) if hold_days else max(1, min(dte - 1, int(dte * 0.6)))
    hold = max(1, min(hold, dte))
    spread_half = ((ask - bid) / 2) if bid and ask else premium * 0.03
    if not (bid and ask):
        notes.append("No live bid/ask: exit cost assumed at 3% of premium per side.")
    fee = float(db.get_settings()["fee_per_contract"])
    cost_total = premium * 100 * qty + fee * qty

    def value(S, t_days, mult=ivm):
        Tleft = max(dte - t_days, 0) / 365
        v = bs_price(np.asarray(S, dtype=float), strike, Tleft, r, iv * mult, kind, q)
        return np.maximum(np.asarray(v) - (spread_half if Tleft > 0 else 0), 0)   # selling at the bid

    def pnl(v):
        proceeds = np.maximum(v * 100 * qty - fee * qty, 0)      # a worthless option is left to expire, not sold
        return proceeds - cost_total

    sd = iv * math.sqrt(hold / 365)
    user_move = float(move_pct) / 100 if move_pct not in (None, "") else None
    scen = [("Bullish", math.exp(1.0 * sd) - 1, "+1 standard move"), ("Base", 0.0, "stock unchanged"),
            ("Bearish", math.exp(-1.0 * sd) - 1, "-1 standard move"),
            ("Severe downside", min(math.exp(-2.0 * sd) - 1, -0.30), "-2 standard moves or -30%")]
    if user_move is not None:
        scen.insert(0, ("Your expectation", user_move, "your input"))
    dates = sorted({max(1, hold // 2), hold, dte})
    table = []
    for name, mv, why in scen:
        S = S0 * (1 + mv)
        cells = []
        for d in dates:
            v = float(value(S, d)) if d < dte else float(value(S, d, 1.0))
            pl = float(pnl(v))
            cells.append(dict(day=d, date=str(dt.date.today() + dt.timedelta(days=d)), option=_r(v), pnl=_r(pl),
                              pnl_pct=_r(pl / cost_total, 4)))
        table.append(dict(name=name, move=_r(mv, 4), stock=_r(S), why=why, cells=cells))

    # Monte Carlo at the holding date: regime mix from the macro engine, fat tails + jumps
    from simulate import simulate_regimes
    from simulate import cal_to_td
    td_h = cal_to_td(hold)
    sims = simulate_regimes(S0, iv, [td_h], max(2000, min(int(paths), 50000)), None, 0.0, seed)
    S_all = np.concatenate([sims[k][td_h][: int(round(w * len(sims[k][td_h])))] for k, w in macro["weights"].items()])
    PL = pnl(value(S_all, hold)) / cost_total
    edges = np.array([-np.inf, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 1.0, 2.0, 1e9])
    labels = ["-100% to -75%", "-75% to -50%", "-50% to -25%", "-25% to 0%", "0% to +25%", "+25% to +50%",
              "+50% to +100%", "+100% to +200%", "over +200%"]
    hist = np.histogram(PL, bins=edges)[0] / len(PL)
    dist = dict(buckets=[dict(label=l, p=_r(p, 4)) for l, p in zip(labels, hist)],
                p_profit=_r(float(np.mean(PL > 0)), 4), p_lose_half=_r(float(np.mean(PL <= -0.5)), 4),
                p_double=_r(float(np.mean(PL >= 1.0)), 4), mean=_r(float(np.mean(PL)), 4),
                median=_r(float(np.median(PL)), 4), n=int(len(PL)),
                stock_q10=_r(float(np.percentile(S_all, 10))), stock_q90=_r(float(np.percentile(S_all, 90))))

    grid = np.linspace(S0 * 0.6, S0 * 1.5, 46)
    curve = dict(stock=[_r(x) for x in grid], hold=[_r(float(x)) for x in pnl(value(grid, hold))],
                 expiry=[_r(float(x)) for x in pnl(value(grid, dte, 1.0))])
    be = strike + premium if kind == "call" else strike - premium
    g = greeks(S0, strike, T, r, iv, kind, q)
    return dict(ticker=ticker, kind=kind, strike=strike, expiry=expiry, dte=dte, qty=qty, hold_days=hold,
                iv_scenario=iv_scenario, inputs=inputs, notes=notes + [
                    "Model values use Black-Scholes with the IV scenario applied; they are estimates, not quotes.",
                    "Monte Carlo mixes 11 market regimes with fat tails and jumps. It does not predict the future."],
                cost=_r(cost_total), max_loss=_r(cost_total), breakeven_expiry=_r(be),
                greeks={k: _r(v, 4) for k, v in g.items()}, scenarios=table, distribution=dist, curve=curve,
                fee_note=f"Fees estimated at ${fee:.2f} per contract each way (set in Settings).")
