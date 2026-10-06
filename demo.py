"""Offline DEMO data. Entirely synthetic, clearly labelled, used only to test the pipeline."""
import datetime as dt
import numpy as np
import pandas as pd
from pricing import bs_price
from data_sources import TickerData, stamp


def _hist(S_end, vol, drift, seed, days=1260):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift / 252, vol / np.sqrt(252), days)
    px = np.exp(np.cumsum(r))
    px = px / px[-1] * S_end
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    return pd.DataFrame({"Close": px, "Volume": rng.integers(5e6, 2e7, days).astype(float)}, index=idx)


def _chain(S, r, base_iv, skew, seed):
    rng = np.random.default_rng(seed)
    today = dt.date.today()
    chains = {}
    step = 1 if S < 60 else 2.5 if S < 150 else 5
    strikes = np.arange(round(S * 0.6 / step) * step, S * 1.5, step)
    for dte in (35, 63, 98, 182, 280, 455):
        exp = str(today + dt.timedelta(days=dte))
        T = dte / 365
        out = {}
        for kind in ("call", "put"):
            rows = []
            for K in strikes:
                iv = base_iv * (1 + skew * (-np.log(K / S))) * (1 + 0.04 * np.sqrt(T))
                mid = float(bs_price(S, K, T, r, iv, kind))
                if mid < 0.05:
                    continue
                w = max(0.02, mid * rng.uniform(0.015, 0.06))
                oi = int(6000 * np.exp(-(np.log(K / S) / 0.18) ** 2) * rng.uniform(0.5, 1.5))
                rows.append(dict(strike=float(K), bid=round(mid - w / 2, 2), ask=round(mid + w / 2, 2),
                                 lastPrice=round(mid, 2), volume=int(oi * 0.1), openInterest=oi,
                                 lastTradeDate=pd.Timestamp.now(tz="UTC")))
            out[kind + "s"] = pd.DataFrame(rows)
        chains[exp] = out
    return chains


SPECS = [  # name, price, real vol, drift, option IV, skew, fundamentals profile, earnings in days
    ("DEMO-A", 180.0, 0.30, 0.45, 0.29, 0.25, "quality", 40),
    ("DEMO-B", 62.0, 0.45, -0.35, 0.58, 0.35, "weak", 20),
    ("DEMO-C", 410.0, 0.25, 0.20, 0.33, 0.20, "quality", None),
    ("DEMO-D", 25.0, 0.70, 0.10, 0.95, 0.30, "weak", 12),
    ("DEMO-E", 95.0, 0.30, 0.35, 0.19, 0.20, "quality", 70),
]
FUND = {
    "quality": dict(revenue=50e9, rev_growth=0.22, net_income=12e9, ni_growth=0.30, fcf=13e9, cash=30e9, debt=10e9, equity=60e9, shares=1.2e9),
    "average": dict(revenue=20e9, rev_growth=0.06, net_income=2e9, ni_growth=0.05, fcf=2.2e9, cash=3e9, debt=8e9, equity=15e9, shares=0.6e9),
    "weak": dict(revenue=5e9, rev_growth=-0.08, net_income=-0.4e9, fcf=-0.6e9, cash=0.5e9, debt=4e9, equity=2e9, shares=0.3e9),
}


def make_universe(r=0.04):
    out = []
    for i, (name, S, vol, drift, iv, skew, prof, earn) in enumerate(SPECS):
        td = TickerData(name)
        td.hist = _hist(S, vol, drift, seed=100 + i)
        td.price, td.price_time = S, stamp() + " (SYNTHETIC)"
        td.chains = _chain(S, r, iv, skew, seed=200 + i)
        td.fundamentals = dict(FUND[prof])
        td.info = {"targetMeanPrice": S * (1.15 if prof == "quality" else 0.95), "numberOfAnalystOpinions": 20}
        td.earnings_date = dt.date.today() + dt.timedelta(days=earn) if earn else None
        td.past_earnings_moves = [0.04, 0.06, 0.03, 0.07] if earn else []
        td.flags = [("MINOR", "DEMO: synthetic data")]
        td.sources = {"DEMO synthetic generator": stamp()}
        out.append(td)
    return out


def make_bench():
    return _hist(500.0, 0.16, 0.10, seed=1)


def make_fred():
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
    f = lambda base, drift: pd.Series(base + np.linspace(0, drift, 300), index=idx)
    return {"DGS10": f(4.2, 0.1), "DGS3MO": f(4.0, -0.2), "T10Y2Y": f(0.3, 0.1), "BAMLH0A0HYM2": f(3.2, -0.1),
            "VIXCLS": f(17, -2), "DTWEXBGS": f(120, 1), "DCOILWTICO": f(75, 3)}
