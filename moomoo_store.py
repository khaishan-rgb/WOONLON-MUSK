"""Server side of the Moomoo integration.

The gateway (moomoo_gateway.py, on the user's machine next to OpenD) pushes signed data here. This module
stores it, turns it into data-provider adapters for the engines, and syncs the user's real Moomoo option
positions into the `positions` table (account='moomoo') - kept separate from manual and paper positions.

Freshness is decided from timestamps, never assumed:
  LIVE          - pushed <= 2 min ago while the US market is open (real-time only if your moomoo account has the
                  US options LV1 quote right; the gateway reports whether quotes came back at all)
  15-MIN DELAY  - pushed 2-15 min ago (treated like other delayed feeds)
  CACHED        - older pushes still inside the configured max age (never used for BUY NOW)
  LAST SESSION  - market closed: last values pushed during/after the session
Anything older is ignored, and the engines fall back to Cboe / Yahoo.
"""
import datetime as dt
import json
import re

import pandas as pd

import config
import db
from market import market_status

OCC = re.compile(r"^US\.([A-Z][A-Z0-9.\-]*?)(\d{6})([CP])(\d{4,})$")


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_sec(ts):
    try:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return (now() - t.to_pydatetime()).total_seconds()
    except Exception:
        return 1e9


def meta(freshness, note="", ts=None, confidence="HIGH"):
    return dict(source="Moomoo OpenD (your gateway)", timestamp=ts or iso(), freshness=freshness,
                confidence=confidence, note=note)


def freshness_for(ts, max_age_min):
    a = age_sec(ts)
    st = market_status()["status"]
    if st != "OPEN":
        return ("LAST SESSION", f"market {st.lower()}; pushed {int(a // 60)} min ago") if a < 4 * 86400 else (None, "")
    if a <= 120:
        return "LIVE", "pushed by gateway < 2 min ago"
    if a <= 15 * 60:
        return "15-MIN DELAY", f"pushed {int(a // 60)} min ago (no older than 15 min)"
    if a <= max_age_min * 60:
        return "CACHED", f"pushed {int(a // 60)} min ago"
    return None, f"too old ({int(a // 60)} min)"


def parse_code(code):
    """US.NVDA270115C900000 -> ('NVDA', '2027-01-15', 'call', 900.0); stocks -> None."""
    m = OCC.match(str(code))
    if not m:
        return None
    sym, ymd, cp, k = m.groups()
    return sym, f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}", "call" if cp == "C" else "put", int(k) / 1000.0


# ------------------------------------------------------------------ ingest (called by signed gateway endpoints)
def ingest_status(payload):
    db.execute("DELETE FROM gateway_status WHERE id NOT IN (SELECT id FROM gateway_status ORDER BY id DESC LIMIT 50)")
    db.insert("gateway_status", dict(ts=iso(), data=db.jdump(payload)))


def ingest_quotes(payload):
    for q in payload.get("quotes", []):
        t = str(q.get("ticker", "")).upper()
        if not t or not q.get("last"):
            continue
        db.execute("DELETE FROM mm_quotes WHERE ticker=?", (t,))
        db.insert("mm_quotes", dict(ticker=t, ts=iso(), data=db.jdump(q)))
    return len(payload.get("quotes", []))


def ingest_chain(payload):
    t, e = str(payload["ticker"]).upper(), str(payload["expiry"])[:10]
    dt.date.fromisoformat(e)
    contracts = [c for c in payload.get("contracts", []) if c.get("strike")]
    db.execute("DELETE FROM mm_chains WHERE ticker=? AND expiry=?", (t, e))
    db.insert("mm_chains", dict(ticker=t, expiry=e, ts=iso(), spot=float(payload.get("spot") or 0),
                                data=db.jdump(contracts)))
    return len(contracts)


def ingest_klines(payload):
    t = str(payload["ticker"]).upper()
    bars = [b for b in payload.get("bars", []) if b.get("c")]
    db.execute("DELETE FROM mm_klines WHERE ticker=?", (t,))
    db.insert("mm_klines", dict(ticker=t, ts=iso(), data=db.jdump(bars)))
    return len(bars)


def ingest_account(payload):
    """Store the account snapshot and upsert real Moomoo option positions (read-only mirror)."""
    db.execute("DELETE FROM mm_account WHERE id NOT IN (SELECT id FROM mm_account ORDER BY id DESC LIMIT 500)")
    db.insert("mm_account", dict(ts=iso(), data=db.jdump(payload)))
    seen = set()
    for p in payload.get("positions", []):
        parsed = parse_code(p.get("code"))
        qty = float(p.get("qty") or 0)
        if not parsed or qty <= 0:
            continue                       # stocks and short legs are shown, not AI-managed
        sym, exp, kind, strike = parsed
        cost = next((float(p[k]) for k in ("average_cost", "diluted_cost", "cost_price")
                     if p.get(k) not in (None, "", "N/A") and float(p[k] or 0) > 0), None)
        if cost is None:
            continue
        code = str(p["code"])
        seen.add(code)
        row = db.one("SELECT id FROM positions WHERE account='moomoo' AND broker_code=? AND status='OPEN'", (code,))
        if row:
            db.update("positions", row["id"], dict(qty=qty, entry_premium=cost))
        else:
            db.insert("positions", dict(account="moomoo", ticker=sym, kind=kind, strike=strike, expiry=exp, qty=qty,
                                        entry_premium=cost, entry_date=str(dt.date.today()), status="OPEN",
                                        realised=0.0, auto=0, broker_code=code, source="moomoo-sync"))
    for r in db.all("SELECT id, broker_code FROM positions WHERE account='moomoo' AND status='OPEN'"):
        if r["broker_code"] not in seen:
            db.update("positions", r["id"], dict(status="CLOSED", exit_ts=iso(),
                                                 exit_reason="No longer in Moomoo account (sync)"))
    return len(seen)


# ------------------------------------------------------------------ what the gateway should fetch next
def work():
    s = db.get_settings()
    held = db.all("SELECT DISTINCT ticker, expiry FROM positions WHERE status='OPEN'")
    tickers = []
    for t in [h["ticker"] for h in held] + [w["ticker"] for w in db.all("SELECT ticker FROM watchlist")] + \
            [u.strip().upper() for u in s["universe"].split(",")]:
        if t and t not in tickers:
            tickers.append(t)
    orders, status = [], []
    real_ok = config.ALLOW_REAL_ORDERS and s["real_orders_enabled"]
    for tk in db.all("SELECT * FROM order_tickets WHERE status='CONFIRMED' ORDER BY id"):
        if age_sec(tk["confirm_ts"]) > 300:
            db.update("order_tickets", tk["id"], dict(status="EXPIRED", note="Not sent within 5 minutes of confirmation"))
            continue
        if tk["env"] == "REAL" and not real_ok:
            db.update("order_tickets", tk["id"], dict(status="BLOCKED", note="Real orders are disabled on the server"))
            continue
        db.update("order_tickets", tk["id"], dict(status="SENT", sent_ts=iso()))
        orders.append(dict(id=tk["id"], env=tk["env"], code=tk["code"], side=tk["side"], qty=tk["qty"],
                           limit_price=tk["limit_price"]))
    for tk in db.all("SELECT id, env, broker_order_id FROM order_tickets WHERE broker_order_id IS NOT NULL "
                     "AND broker_status NOT IN ('FILLED_ALL','CANCELLED_ALL','FAILED','DELETED','CANCELLED_PART')"):
        status.append(dict(id=tk["id"], env=tk["env"], broker_order_id=tk["broker_order_id"]))
    need_k = [t for t in tickers[: s["mm_max_tickers"]] if not _klines_fresh(t)]
    return dict(tickers=tickers[: s["mm_max_tickers"]], max_tickers=s["mm_max_tickers"], sync_sec=s["mm_sync_sec"],
                window=dict(min_dte=7, max_dte=560), max_expiries=4, chains_per_cycle=3,
                contracts=[dict(ticker=h["ticker"], expiry=h["expiry"]) for h in held],
                kline_tickers=need_k, orders=orders, order_status=status, server_time=iso())


def _klines_fresh(t):
    r = db.one("SELECT ts FROM mm_klines WHERE ticker=?", (t,))
    return bool(r) and age_sec(r["ts"]) < 20 * 3600


def ingest_order_result(p):
    tk = db.one("SELECT * FROM order_tickets WHERE id=?", (int(p["ticket_id"]),))
    if not tk:
        return
    upd = dict(broker_status=str(p.get("status", "")), broker_response=db.jdump(p))
    if p.get("order_id"):
        upd["broker_order_id"] = p["order_id"]
    if not p.get("update"):
        upd["status"] = "SUBMITTED" if p.get("ok") else ("BLOCKED" if p.get("status") == "BLOCKED" else "REJECTED")
    st = str(p.get("status", ""))
    if st.startswith("FILLED"):
        upd["status"] = "FILLED" if st == "FILLED_ALL" else "PARTIALLY FILLED"
    db.update("order_tickets", tk["id"], upd)
    from services import alert
    alert("broker", tk["ticker"], f"{tk['ticker']} order #{tk['id']}: {upd.get('status', tk['status'])}",
          str(p.get("message", ""))[:200], 70, f"order{tk['id']}:{st}")


# ------------------------------------------------------------------ status for the UI
def gateway_info():
    r = db.one("SELECT ts, data FROM gateway_status ORDER BY id DESC LIMIT 1")
    acct = db.one("SELECT ts, data FROM mm_account ORDER BY id DESC LIMIT 1")
    if not r:
        return dict(connected=False, configured=bool(config.GATEWAY_TOKEN), last_seen=None, caps={}, account=None,
                    account_ts=None, chains=0)
    a = age_sec(r["ts"])
    return dict(connected=a < 180, configured=bool(config.GATEWAY_TOKEN), last_seen=r["ts"], age_sec=int(a),
                caps=db.jload(r["data"], {}).get("caps", {}),
                account=db.jload(acct["data"]) if acct else None, account_ts=acct["ts"] if acct else None,
                chains=db.one("SELECT COUNT(*) AS n FROM mm_chains")["n"])


def latest_funds():
    r = db.one("SELECT ts, data FROM mm_account ORDER BY id DESC LIMIT 1")
    if not r:
        return None, None
    return (db.jload(r["data"], {}) or {}).get("funds"), r["ts"]


# ------------------------------------------------------------------ provider adapters
class MoomooBridge:
    """Options + quotes + history adapter backed by gateway pushes. Returns None when nothing fresh exists,
    so FailoverOptions moves on to Cboe / Yahoo."""
    name = "Moomoo OpenD"

    def _max_age(self):
        try:
            return db.get_settings()["mm_chain_max_age_min"]
        except Exception:
            return 30

    def expiries(self, sym):
        rows = db.all("SELECT expiry, ts FROM mm_chains WHERE ticker=?", (sym.upper(),))
        good = [r["expiry"] for r in rows if freshness_for(r["ts"], self._max_age())[0]]
        if not good:
            return None, meta("UNAVAILABLE", "no fresh chain pushed by the gateway", confidence="NONE")
        return sorted(good), meta("CACHED")

    def chain(self, sym, exp):
        r = db.one("SELECT ts, data FROM mm_chains WHERE ticker=? AND expiry=?", (sym.upper(), exp))
        if not r:
            return None, meta("UNAVAILABLE", "expiry not pushed", confidence="NONE")
        fr, note = freshness_for(r["ts"], self._max_age())
        if not fr:
            return None, meta("UNAVAILABLE", note, confidence="NONE")
        cs = db.jload(r["data"], [])
        def frame(kind):
            rows = [dict(contractSymbol=c["code"], strike=float(c["strike"]), bid=c.get("bid") or 0.0,
                         ask=c.get("ask") or 0.0, lastPrice=c.get("last"),
                         change=(c["last"] - c["prev_close"]) if c.get("last") and c.get("prev_close") else None,
                         volume=c.get("volume") or 0.0, openInterest=c.get("oi") or 0.0,
                         impliedVolatility=c.get("iv"), lastTradeDate=None) for c in cs if c.get("kind") == kind]
            return pd.DataFrame(rows).sort_values("strike").reset_index(drop=True) if rows else \
                pd.DataFrame(columns=["contractSymbol", "strike", "bid", "ask", "lastPrice", "change", "volume",
                                      "openInterest", "impliedVolatility", "lastTradeDate"])
        calls, puts = frame("call"), frame("put")
        live = ((calls["bid"] > 0) & (calls["ask"] > 0)).mean() if len(calls) else 0
        if live < 0.2:
            return None, meta("UNAVAILABLE", "no bid/ask in pushed chain (quote right?)", confidence="NONE")
        return {"calls": calls, "puts": puts}, meta(fr, note, r["ts"])

    def quote(self, sym):
        r = db.one("SELECT ts, data FROM mm_quotes WHERE ticker=?", (sym.upper(),))
        if not r:
            return None, meta("UNAVAILABLE", "not pushed", confidence="NONE")
        fr, note = freshness_for(r["ts"], 5)
        if not fr:
            return None, meta("UNAVAILABLE", note, confidence="NONE")
        q = db.jload(r["data"], {})
        return (float(q["last"]), float(q.get("prev_close") or q["last"])), meta(fr, note, r["ts"])

    def history(self, sym, period="5y"):
        r = db.one("SELECT ts, data FROM mm_klines WHERE ticker=?", (sym.upper(),))
        bars = db.jload(r["data"], []) if r else []
        if len(bars) < 60:
            return None, meta("UNAVAILABLE", "no candles pushed", confidence="NONE")
        df = pd.DataFrame(bars)
        df.index = pd.to_datetime(df.pop("d"))
        df = df.rename(columns=dict(o="Open", h="High", l="Low", c="Close", v="Volume")).sort_index()
        return df, meta("CACHED", "daily candles from moomoo", r["ts"])


class FailoverStock:
    """Stock prices: fresh Moomoo quote first, else Yahoo; history: Yahoo, else Moomoo candles."""
    def __init__(self, mm, yahoo):
        self.mm, self.y = mm, yahoo
        self.name = "Moomoo OpenD -> Yahoo Finance"

    def quote(self, sym):
        v, m = self.mm.quote(sym)
        return (v, m) if v else self.y.quote(sym)

    def history(self, sym, period="5y"):
        v, m = self.y.history(sym, period)
        if v is not None:
            return v, m
        v2, m2 = self.mm.history(sym, period)
        return (v2, m2) if v2 is not None else (v, m)

    def __getattr__(self, k):            # info / earnings / expiries pass through to Yahoo
        return getattr(self.y, k)
