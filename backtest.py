"""Backtesting & AI validation (master spec section 10).

HONEST SCOPE - read before trusting any number:
  * None of the connected free sources (Yahoo, Cboe delayed, FRED, EDGAR) provide HISTORICAL OPTION QUOTES, and the
    Moomoo option-candle quota (20-60 option chains per 7 days for most accounts) is far too small for a backtest.
  * So option prices here are MODELLED: Black-Scholes on the stock's real historical prices, with implied
    volatility = trailing 60-day realised volatility x an IV markup (default 1.10, the usual IV>RV premium),
    plus bid/ask slippage and per-contract fees. Today's option chain is never used for past trades.
  * Signals only use data available on the entry day (no look-ahead). Thresholds are chosen on a training
    window and then tested on the NEXT, unseen window (walk-forward / out-of-sample). Only the unseen windows
    are reported as results.
  * The ticker list is today's - companies that failed or were delisted are missing (survivorship bias), which
    flatters results. Treat everything as a stress test of the rules, not proof of profit.
"""
import datetime as dt
import math

import numpy as np
import pandas as pd

from pricing import bs_price

DEFAULTS = dict(tickers="AAPL,MSFT,NVDA,AMZN,GOOGL,META,JPM,XOM,LLY,COST", dte=365, target_delta=0.55,
                hold_days=200, profit_target=1.0, stop_loss=0.6, iv_markup=1.10, half_spread=0.03,
                fee_per_contract=0.65, rebalance_days=21, train_years=2.0, test_years=1.0,
                thresholds="50,60,70,80", risk_per_trade=500.0, start_capital=10000.0, r=0.04)


# ------------------------------------------------------------------ point-in-time signal (vectorised, no look-ahead)
def tech_score_series(close, bench):
    c = close.astype(float)
    s20, s50, s200 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    mh = macd - macd.ewm(span=9, adjust=False).mean()
    b = bench.reindex(c.index).ffill()
    rs = c.pct_change(63) - b.pct_change(63)
    sc = 15 + 10 * (c > s20) + 10 * (c > s50) + 15 * (c > s200) + 10 * (s50 > s200) + 10 * ((rsi >= 50) & (rsi <= 70)) \
        - 10 * (rsi > 78) + 10 * (mh > 0) + 10 * (rs > 0) + 5 * (c >= 0.95 * c.rolling(252).max())
    return sc.where(s200.notna())


def strike_for_delta(S, T, r, sigma, delta):
    from scipy.stats import norm
    d1 = norm.ppf(delta)
    return S * math.exp(-(d1 * sigma * math.sqrt(T)) + (r + 0.5 * sigma ** 2) * T)


def _sim_trade(c, rv, i, P, r):
    """One modelled long call opened at index i. Returns dict or None if not enough future data."""
    S0, sig0 = float(c.iloc[i]), float(rv.iloc[i]) * P["iv_markup"]
    if not sig0 or math.isnan(sig0):
        return None
    T0 = P["dte"] / 365
    K = round(strike_for_delta(S0, T0, r, sig0, P["target_delta"]), 2)
    mid0 = float(bs_price(S0, K, T0, r, sig0, "call"))
    if mid0 < 0.2:
        return None
    entry = mid0 * (1 + P["half_spread"])
    cost = entry * 100 + P["fee_per_contract"]
    last_i = min(i + int(P["hold_days"] * 252 / 365), len(c) - 1)
    if last_i - i < 5:
        return None
    reason, exit_i, val = "time exit", last_i, None
    for j in range(i + 1, last_i + 1):
        days = (c.index[j] - c.index[i]).days
        sig = float(rv.iloc[j]) * P["iv_markup"] if not math.isnan(rv.iloc[j]) else sig0
        v = float(bs_price(float(c.iloc[j]), K, max(P["dte"] - days, 0) / 365, r, sig, "call")) * (1 - P["half_spread"])
        ret = (v * 100 - P["fee_per_contract"]) / cost - 1
        if ret >= P["profit_target"]:
            reason, exit_i, val = "profit target", j, v
            break
        if ret <= -P["stop_loss"]:
            reason, exit_i, val = "max-loss rule", j, v
            break
        val = v
    if exit_i == last_i and last_i == len(c) - 1 and (c.index[-1] - c.index[i]).days < P["hold_days"] * 0.9:
        return None                 # trade still open at the end of data - excluded, not guessed
    pnl = (val * 100 - P["fee_per_contract"]) - cost
    return dict(entry_date=str(c.index[i].date()), exit_date=str(c.index[exit_i].date()), S0=round(S0, 2), K=K,
                entry=round(entry, 2), exit=round(val, 2), ret=pnl / cost, pnl_per_contract=pnl, cost=cost,
                reason=reason, days=(c.index[exit_i] - c.index[i]).days)


def _metrics(trades, P):
    if not trades:
        return dict(n=0)
    t = sorted(trades, key=lambda x: x["exit_date"])
    rets = np.array([x["ret"] for x in t])
    usd = rets * P["risk_per_trade"]
    eq, peak, mdd, curve = P["start_capital"], P["start_capital"], 0.0, []
    for x, u in zip(t, usd):
        eq += u
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        curve.append(dict(d=x["exit_date"], eq=round(eq, 2)))
    wins, losses = rets[rets > 0], rets[rets <= 0]
    span_y = max((pd.Timestamp(t[-1]["exit_date"]) - pd.Timestamp(min(x["entry_date"] for x in t))).days / 365, 0.25)
    # capital usage: peak premium tied up in simultaneously open trades
    ev = sorted([(x["entry_date"], P["risk_per_trade"]) for x in t] + [(x["exit_date"], -P["risk_per_trade"]) for x in t])
    used, peak_used = 0.0, 0.0
    for _, a in ev:
        used += a
        peak_used = max(peak_used, used)
    sr = float(np.mean(rets) / np.std(rets) * math.sqrt(len(rets) / span_y)) if len(rets) > 2 and np.std(rets) > 0 else None
    return dict(n=len(t), total_return=(eq - P["start_capital"]) / P["start_capital"], win_rate=float(np.mean(rets > 0)),
                avg_win=float(np.mean(wins)) if len(wins) else None, avg_loss=float(np.mean(losses)) if len(losses) else None,
                median=float(np.median(rets)), mean=float(np.mean(rets)),
                profit_factor=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
                max_drawdown=mdd, risk_adjusted=sr, capital_peak=peak_used,
                capital_peak_pct=peak_used / P["start_capital"], costs_per_trade=P["fee_per_contract"] * 2,
                curve=curve[-300:])


def run(params=None, history_fn=None, progress=None):
    P = dict(DEFAULTS)
    P.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
    for k, v in DEFAULTS.items():
        if isinstance(v, float) and not isinstance(P[k], float):
            P[k] = float(P[k])
        if isinstance(v, int) and not isinstance(v, bool):
            P[k] = int(float(P[k]))
    tickers = [t.strip().upper() for t in str(P["tickers"]).split(",") if t.strip()][:25]
    ths = [float(x) for x in str(P["thresholds"]).split(",") if x.strip()]
    if history_fn is None:
        from providers import providers
        history_fn = lambda s: providers().stock.history(s)[0]
    bench = history_fn("SPY")
    if bench is None or len(bench) < 400:
        return dict(error="Not enough SPY history to run a backtest (data source unavailable?)")
    bc = bench["Close"].astype(float)
    bc.index = pd.DatetimeIndex(bc.index).tz_localize(None) if bc.index.tz is not None else pd.DatetimeIndex(bc.index)
    regime = pd.Series(np.where(bc > bc.rolling(200).mean(), "SPY uptrend", "SPY downtrend"), index=bc.index)

    signal_trades, base_trades, stock_bh, skipped = [], [], {}, []
    for n_t, sym in enumerate(tickers):
        if progress:
            progress(n_t, len(tickers), sym)
        h = history_fn(sym)
        if h is None or len(h) < 400:
            skipped.append(sym)
            continue
        c = h["Close"].astype(float).dropna()
        c.index = pd.DatetimeIndex(c.index).tz_localize(None) if c.index.tz is not None else pd.DatetimeIndex(c.index)
        rv = np.log(c / c.shift(1)).rolling(60).std() * math.sqrt(252)
        sc = tech_score_series(c, bc)
        stock_bh[sym] = float(c.iloc[-1] / c.iloc[200] - 1)
        for i in range(200, len(c) - 5, int(P["rebalance_days"])):
            tr = _sim_trade(c, rv, i, P, P["r"])
            if not tr:
                continue
            tr.update(ticker=sym, score=float(sc.iloc[i]) if not math.isnan(sc.iloc[i]) else None,
                      regime=str(regime.reindex([c.index[i]], method="ffill").iloc[0]) if c.index[i] >= regime.index[0] else "?")
            base_trades.append(tr)

    # walk-forward: choose the threshold on [train], apply to the following [test] window only
    if not base_trades:
        return dict(error="No trades could be simulated", skipped=skipped)
    start = pd.Timestamp(min(t["entry_date"] for t in base_trades))
    end = pd.Timestamp(max(t["entry_date"] for t in base_trades))
    folds, cur = [], start + pd.DateOffset(days=int(P["train_years"] * 365))
    while cur < end:
        tr_lo, te_hi = cur - pd.DateOffset(days=int(P["train_years"] * 365)), cur + pd.DateOffset(days=int(P["test_years"] * 365))
        # training trades must have EXITED before the test window starts (no leakage)
        train = [t for t in base_trades if tr_lo <= pd.Timestamp(t["entry_date"]) and pd.Timestamp(t["exit_date"]) < cur]
        best, best_val = None, -1e9
        for th in ths:
            sel = [t["ret"] for t in train if t["score"] is not None and t["score"] >= th]
            if len(sel) >= 5 and np.mean(sel) > best_val:
                best, best_val = th, float(np.mean(sel))
        test = [t for t in base_trades if cur <= pd.Timestamp(t["entry_date"]) < te_hi]
        chosen = [t for t in test if best is not None and t["score"] is not None and t["score"] >= best]
        signal_trades += chosen
        folds.append(dict(train_from=str(tr_lo.date()), test_from=str(cur.date()), test_to=str(min(te_hi, end).date()),
                          threshold=best, train_mean=round(best_val, 4) if best is not None else None,
                          test_trades=len(chosen), test_mean=round(float(np.mean([t["ret"] for t in chosen])), 4) if chosen else None))
        cur = te_hi
    oos_from = folds[0]["test_from"] if folds else None
    base_oos = [t for t in base_trades if oos_from and t["entry_date"] >= oos_from]
    by_regime = {}
    for rg in ("SPY uptrend", "SPY downtrend"):
        by_regime[rg] = _metrics([t for t in signal_trades if t["regime"] == rg], P)
        by_regime[rg].pop("curve", None)
    spy_oos = None
    if oos_from:
        b = bc[bc.index >= pd.Timestamp(oos_from)]
        spy_oos = float(b.iloc[-1] / b.iloc[0] - 1) if len(b) > 1 else None
    res = dict(
        params=P, generated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), skipped=skipped,
        option_prices="MODELLED (Black-Scholes on real stock history; IV = 60-day realised vol x markup). "
                      "No historical option quotes were available.",
        out_of_sample_from=oos_from, folds=folds,
        strategy=_metrics(signal_trades, P),
        benchmark_all_calls=_metrics(base_oos, P),
        benchmark_spy_buy_hold=spy_oos,
        benchmark_stocks_buy_hold=float(np.mean(list(stock_bh.values()))) if stock_bh else None,
        by_regime=by_regime,
        exit_reasons={k: sum(1 for t in signal_trades if t["reason"] == k) for k in ("profit target", "max-loss rule", "time exit")},
        sample_trades=sorted(signal_trades, key=lambda t: t["entry_date"])[-40:],
        warnings=["Survivorship bias: today's tickers only.",
                  "Option prices are modelled, so real fills, IV spikes and earnings IV crush are only approximated.",
                  "Few trades = LOW CONFIDENCE." if len(signal_trades) < 30 else "Sample size moderate - still not proof."],
    )
    s, b = res["strategy"], res["benchmark_all_calls"]
    if not s.get("n"):
        res["verdict"] = "Not enough out-of-sample trades to judge."
    elif s["n"] < 30:
        res["verdict"] = (f"LOW CONFIDENCE: only {s['n']} out-of-sample trades - far too few to claim the signal works "
                          "(or doesn't). Add tickers or a longer history.")
    else:
        better = b.get("n") and s["mean"] > b["mean"] and (s.get("profit_factor") or 0) > (b.get("profit_factor") or 0)
        res["verdict"] = ("Per trade, the signal filter did better than buying the same calls every time (out of sample)."
                          if better else "The signal filter did NOT beat simply buying the same calls - no evidence of an edge.")
    if s.get("n") and s["mean"] <= 0:
        res["verdict"] += " The average modelled trade lost money: this strategy is not shown to be profitable."
    return res
