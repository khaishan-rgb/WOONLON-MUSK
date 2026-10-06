"""Free data layer (spec 1). Every source is timestamped; problems become flags, never invented data.

Sources:
  Yahoo Finance via yfinance  - prices, history, option chains (unofficial, ~15 min delayed)
  FRED CSV (no key)           - yields, curve, credit spreads, VIX, USD, oil
  SEC EDGAR XBRL (no key)     - audited fundamentals, shares outstanding
  Finnhub (free key, optional)- second price source for cross-checking, company news
"""
import datetime as dt
import io
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

import config


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def stamp():
    return now_utc().strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class TickerData:
    ticker: str
    price: float = None
    price_time: str = None
    hist: pd.DataFrame = None
    chains: dict = field(default_factory=dict)      # expiry 'YYYY-MM-DD' -> {"calls": df, "puts": df}
    info: dict = field(default_factory=dict)
    fundamentals: dict = field(default_factory=dict)
    earnings_date: dt.date = None
    past_earnings_moves: list = field(default_factory=list)
    news: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)        # (severity, message); severity in {"SEVERE","MINOR"}
    sources: dict = field(default_factory=dict)
    is_etf: bool = False
    dividend_yield: float = 0.0


# ---------------------------------------------------------------- Yahoo
def fetch_yahoo(ticker):
    try:
        import yfinance as yf
    except ImportError:
        raise SystemExit("yfinance missing. Run: pip install -r requirements.txt")
    td = TickerData(ticker)
    t = yf.Ticker(ticker)
    hist = t.history(period="5y", auto_adjust=True)
    if hist is None or hist.empty:
        td.flags.append(("SEVERE", "No price history from Yahoo"))
        return td
    td.hist = hist
    td.price = float(hist["Close"].iloc[-1])
    td.price_time = str(hist.index[-1].date())
    try:
        lp = t.fast_info.last_price
        if lp and lp > 0:
            td.price = float(lp)
            td.price_time = stamp() + " (delayed ~15m)"
    except Exception:
        pass
    td.sources["Yahoo prices/options"] = stamp()
    td.flags.append(("MINOR", "Yahoo data is free but delayed (~15 min) and unofficial"))

    try:
        td.info = t.info or {}
    except Exception:
        td.info = {}
    td.is_etf = td.info.get("quoteType") == "ETF"
    dy = td.info.get("dividendYield") or td.info.get("yield") or 0.0
    td.dividend_yield = float(dy) / 100 if dy and dy > 0.2 else float(dy or 0.0)

    # option chains: spread the expiries across the allowed window
    today = dt.date.today()
    exps = []
    for e in t.options or []:
        dte = (dt.date.fromisoformat(e) - today).days
        if config.MIN_DTE <= dte <= config.MAX_DTE:
            exps.append(e)
    if len(exps) > config.MAX_EXPIRIES:
        idx = np.linspace(0, len(exps) - 1, config.MAX_EXPIRIES).round().astype(int)
        exps = [exps[i] for i in sorted(set(idx))]
    for e in exps:
        try:
            ch = t.option_chain(e)
            td.chains[e] = {"calls": ch.calls.copy(), "puts": ch.puts.copy()}
            time.sleep(config.REQUEST_PAUSE)
        except Exception as ex:
            td.flags.append(("MINOR", f"Chain {e} failed: {ex}"))
    if not td.chains:
        td.flags.append(("SEVERE", "No option chains in 30-540 DTE window"))

    # earnings (next date + historical post-earnings moves)
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            fut = [d for d in dates if d >= today]
            td.earnings_date = fut[0] if fut else None
    except Exception:
        pass
    try:
        ed = t.get_earnings_dates(limit=16)
        closes = hist["Close"].copy()
        closes.index = closes.index.date
        idx = list(closes.index)
        for ts in ed.index:
            d = ts.date()
            if d >= today:
                continue
            pos = next((i for i, x in enumerate(idx) if x >= d), None)
            if pos and 0 < pos < len(idx) - 1:
                td.past_earnings_moves.append(abs(closes.iloc[pos + 1] / closes.iloc[pos - 1] - 1))
    except Exception:
        pass
    return td


def fetch_history_only(ticker):
    import yfinance as yf
    return yf.Ticker(ticker).history(period="5y", auto_adjust=True)


# ---------------------------------------------------------------- FRED
FRED_SERIES = ["DGS10", "DGS3MO", "T10Y2Y", "BAMLH0A0HYM2", "VIXCLS", "DTWEXBGS", "DCOILWTICO"]


def fetch_fred(series=FRED_SERIES):
    out = {}
    for sid in series:
        try:
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
            df = pd.read_csv(io.StringIO(requests.get(url, timeout=config.HTTP_TIMEOUT).text))
            s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                          index=pd.to_datetime(df.iloc[:, 0])).dropna()
            out[sid] = s
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- SEC EDGAR
_CIK = None


def _sec_get(url):
    r = requests.get(url, headers={"User-Agent": config.SEC_USER_AGENT}, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _cik(ticker):
    global _CIK
    if _CIK is None:
        data = _sec_get("https://www.sec.gov/files/company_tickers.json")
        _CIK = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
    return _CIK.get(ticker.upper().replace("-", "."))


def _annual(facts, tags, unit="USD", duration=True):
    """Latest annual values for the first tag that has the freshest data."""
    best = []
    for tag in tags:
        node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if not node:
            continue
        vals = {}
        for r in node.get("units", {}).get(unit, []):
            if r.get("form") not in ("10-K", "10-K/A", "20-F", "40-F"):
                continue
            if duration:
                if not r.get("start"):
                    continue
                days = (pd.Timestamp(r["end"]) - pd.Timestamp(r["start"])).days
                if not 330 <= days <= 400:
                    continue
            vals[r["end"]] = r["val"]
        if vals:
            ser = sorted(vals.items())
            if not best or ser[-1][0] > best[-1][0]:
                best = ser
    return best


def parse_edgar(facts):
    rev = _annual(facts, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                          "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"])
    ni = _annual(facts, ["NetIncomeLoss"])
    ocf = _annual(facts, ["NetCashProvidedByUsedInOperatingActivities"])
    capex = _annual(facts, ["PaymentsToAcquirePropertyPlantAndEquipment"])
    cash = _annual(facts, ["CashAndCashEquivalentsAtCarryingValue",
                           "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], duration=False)
    debt = _annual(facts, ["LongTermDebt", "LongTermDebtNoncurrent"], duration=False)
    eq = _annual(facts, ["StockholdersEquity"], duration=False)
    f = {}
    if len(rev) >= 2 and rev[-2][1]:
        f["revenue"], f["rev_growth"] = rev[-1][1], rev[-1][1] / rev[-2][1] - 1
        f["fiscal_year_end"] = rev[-1][0]
    elif rev:
        f["revenue"] = rev[-1][1]
    if ni:
        f["net_income"] = ni[-1][1]
        if len(ni) >= 2 and ni[-2][1] > 0:
            f["ni_growth"] = ni[-1][1] / ni[-2][1] - 1
    if ocf:
        f["fcf"] = ocf[-1][1] - (capex[-1][1] if capex else 0)
    f["cash"] = cash[-1][1] if cash else 0.0
    f["debt"] = debt[-1][1] if debt else 0.0
    if eq:
        f["equity"] = eq[-1][1]
    try:
        sh = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
        f["shares"] = sorted(sh, key=lambda r: r["end"])[-1]["val"]
    except Exception:
        pass
    return f


def fetch_edgar(ticker):
    cik = _cik(ticker)
    if not cik:
        return {}
    facts = _sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    return parse_edgar(facts)


# ---------------------------------------------------------------- Finnhub (optional)
POS = ("beat", "raise", "upgrade", "record", "surge", "approval", "approved", "wins", "contract", "buyback", "partnership")
NEG = ("miss", "cut", "downgrade", "lawsuit", "probe", "recall", "plunge", "investigation", "delay", "warning", "fraud")


def fetch_finnhub(ticker):
    if not config.FINNHUB_KEY:
        return None, {}
    base, k = "https://finnhub.io/api/v1", config.FINNHUB_KEY
    quote, news = None, {}
    try:
        quote = requests.get(f"{base}/quote", params={"symbol": ticker, "token": k}, timeout=config.HTTP_TIMEOUT).json().get("c")
    except Exception:
        pass
    try:
        to = dt.date.today()
        items = requests.get(f"{base}/company-news", params={"symbol": ticker, "from": str(to - dt.timedelta(days=14)),
                             "to": str(to), "token": k}, timeout=config.HTTP_TIMEOUT).json()
        heads = [i.get("headline", "").lower() for i in items[:60]]
        p = sum(any(w in h for w in POS) for h in heads)
        n = sum(any(w in h for w in NEG) for h in heads)
        news = dict(count=len(heads), positive=p, negative=n, sentiment=(p - n) / max(1, p + n))
    except Exception:
        pass
    return quote, news


# ---------------------------------------------------------------- orchestration
def load_ticker(ticker):
    td = fetch_yahoo(ticker)
    if td.price is None:
        return td
    if not td.is_etf:
        try:
            td.fundamentals = fetch_edgar(ticker)
            td.sources["SEC EDGAR"] = stamp()
        except Exception as ex:
            td.flags.append(("MINOR", f"EDGAR fundamentals unavailable ({ex.__class__.__name__})"))
        if not td.fundamentals:
            td.flags.append(("MINOR", "No EDGAR fundamentals found"))
    quote, news = fetch_finnhub(ticker)
    if quote:
        td.sources["Finnhub"] = stamp()
        diff = abs(quote / td.price - 1)
        if diff > 0.015:
            td.flags.append(("SEVERE", f"PRICE CONFLICT: Yahoo {td.price:.2f} vs Finnhub {quote:.2f}"))
    else:
        td.flags.append(("MINOR", "Price not cross-validated (set FINNHUB_API_KEY)"))
    td.news = news
    last = pd.Timestamp(td.hist.index[-1]).tz_localize(None) if td.hist.index.tz else pd.Timestamp(td.hist.index[-1])
    if (pd.Timestamp.now() - last).days > 4:
        td.flags.append(("SEVERE", f"STALE price history (last bar {last.date()})"))
    return td
