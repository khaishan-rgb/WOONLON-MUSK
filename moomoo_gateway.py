"""SENTRY Moomoo Gateway - run this on YOUR computer (or a small always-on server) next to moomoo OpenD.

Why a separate program?
  Moomoo's API works through OpenD, a gateway app you log in to with your moomoo account (it can ask for a
  phone verification code). It speaks TCP on 127.0.0.1:11111. A Render web service cannot run OpenD reliably
  (no login prompt, restarts, no fixed device). So OpenD + this script run where you can log in, and this
  script PUSHES data to your SENTRY website over HTTPS, signed with a shared secret. Your moomoo password and
  trade password never leave this machine. Render never connects into your network.

What it does each cycle (all read-only unless you explicitly enable orders):
  1. asks SENTRY which tickers it needs              GET  /api/gateway/work
  2. account: funds, positions, recent deals          POST /api/gateway/account   (accinfo_query, position_list_query)
  3. stock snapshots for those tickers                POST /api/gateway/quotes    (get_market_snapshot, <=400 codes)
  4. option chains + quotes/Greeks, round-robin       POST /api/gateway/chain     (get_option_expiration_date,
                                                                                   get_option_chain, get_market_snapshot)
  5. daily candles (within your 7-day kline quota)    POST /api/gateway/klines    (request_history_kline)
  6. orders you CONFIRMED in SENTRY - only if MOOMOO_ALLOW_REAL_ORDERS=1 here AND real orders are enabled on
     the server. Moomoo paper-trading tickets (TrdEnv.SIMULATE) need no trade password.
  7. heartbeat with detected capabilities             POST /api/gateway/status

Setup (once):
  1. Install and log in to moomoo OpenD:  https://www.moomoo.com/download/OpenAPI   (keep it running)
  2. pip install moomoo-api requests pandas
  3. Set environment variables (same GATEWAY_TOKEN as on Render):
       SENTRY_URL=https://your-app.onrender.com
       GATEWAY_TOKEN=<long random secret>
       OPEND_HOST=127.0.0.1   OPEND_PORT=11111       (defaults)
       MOOMOO_SECURITY_FIRM=FUTUSG                    (moomoo Singapore; FUTUINC = moomoo US)
  4. python moomoo_gateway.py --selftest     (checks OpenD, quote rights and account access; sends nothing)
     python moomoo_gateway.py                (runs continuously; Ctrl+C to stop)

API limits respected (from the official docs): get_option_chain 10 requests / 30 s and max 30-day span;
get_market_snapshot 60 requests / 30 s and 400 codes per request; place_order 15 / 30 s per account.
"""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import logging
import math
import os
import sys
import time

log = logging.getLogger("sentry-gateway")


def _load_env_file():
    """Optional: put settings in gateway.env next to this file (KEY=VALUE per line). Real env vars win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway.env")
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

ENV = dict(
    url=os.getenv("SENTRY_URL", "").rstrip("/"),
    token=os.getenv("GATEWAY_TOKEN", ""),
    host=os.getenv("OPEND_HOST", "127.0.0.1"),
    port=int(os.getenv("OPEND_PORT", "11111")),
    firm=os.getenv("MOOMOO_SECURITY_FIRM", "FUTUSG"),
    acc_id=int(os.getenv("MOOMOO_ACC_ID", "0") or 0),
    trade_pwd=os.getenv("MOOMOO_TRADE_PWD", ""),            # only needed for REAL orders; stays on this machine
    allow_real=os.getenv("MOOMOO_ALLOW_REAL_ORDERS", "0") == "1",
)


# ------------------------------------------------------------------ helpers
def num(x):
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def iv_frac(x):
    """Moomoo reports implied volatility in percent form (e.g. 34.2 = 34.2%)."""
    v = num(x)
    if v is None or v <= 0:
        return None
    return v / 100 if v > 3 else v


def rows(df):
    return [] if df is None or not hasattr(df, "to_dict") else df.to_dict("records")


def utcnow():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Spacer:
    """Minimum spacing between calls so we stay under each documented frequency limit."""
    def __init__(self, seconds):
        self.s, self.t = seconds, 0.0

    def wait(self):
        gap = self.s - (time.time() - self.t)
        if gap > 0:
            time.sleep(gap)
        self.t = time.time()


CHAIN_SPACER = Spacer(3.2)      # 10 / 30 s
SNAP_SPACER = Spacer(0.55)      # 60 / 30 s
KLINE_SPACER = Spacer(1.0)
ORDER_SPACER = Spacer(2.1)      # 15 / 30 s


# ------------------------------------------------------------------ SENTRY client (signed)
class Sentry:
    def __init__(self, url, token, dry_run=False):
        import requests
        self.req, self.url, self.token, self.dry = requests, url, token, dry_run

    def _headers(self, method, path, body):
        ts, nonce = str(int(time.time())), os.urandom(12).hex()
        msg = "\n".join([ts, nonce, method, path, hashlib.sha256(body).hexdigest()])
        sig = hmac.new(self.token.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {"X-Sentry-Ts": ts, "X-Sentry-Nonce": nonce, "X-Sentry-Sig": sig, "Content-Type": "application/json"}

    def call(self, method, path, payload=None):
        body = json.dumps(payload, default=str).encode() if payload is not None else b""
        if self.dry and method == "POST":
            log.info("DRY-RUN %s %s (%d bytes)", method, path, len(body))
            return {}
        for attempt in range(3):
            try:
                r = self.req.request(method, self.url + path, data=body or None, timeout=30,
                                     headers=self._headers(method, path, body))
                if r.status_code >= 500 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    log.warning("SENTRY %s %s -> %s %s", method, path, r.status_code, r.text[:200])
                    return None
                return r.json()
            except Exception as ex:          # network blips, Render cold start
                log.warning("SENTRY %s %s failed (%s), retry %d", method, path, ex, attempt + 1)
                time.sleep(3 * (attempt + 1))
        return None


# ------------------------------------------------------------------ OpenD
class OpenD:
    def __init__(self, host, port, firm):
        import moomoo as mm                 # official SDK: pip install moomoo-api
        self.mm, self.host, self.port = mm, host, port
        self.firm = getattr(mm.SecurityFirm, firm, mm.SecurityFirm.FUTUSG)
        self.q = self.t = None
        self.caps = dict(quote=None, options=None, account=None, trade_unlocked=False, errors=[])

    def connect(self):
        mm = self.mm
        if hasattr(mm, "set_all_thread_daemon"):
            mm.set_all_thread_daemon(True)
        self.close()
        self.q = mm.OpenQuoteContext(host=self.host, port=self.port)
        try:
            self.t = mm.OpenSecTradeContext(filter_trdmarket=mm.TrdMarket.US, host=self.host, port=self.port,
                                            security_firm=self.firm)
        except Exception as ex:
            self.t = None
            self._err(f"trade context: {ex}")

    def close(self):
        for c in (self.q, self.t):
            try:
                c and c.close()
            except Exception:
                pass
        self.q = self.t = None

    def _err(self, msg):
        log.warning(msg)
        self.caps["errors"] = (self.caps["errors"] + [f"{utcnow()} {msg}"])[-20:]

    def ok(self, ret):
        return ret == self.mm.RET_OK

    # ---- quotes
    def snapshot(self, codes):
        out = []
        for i in range(0, len(codes), 400):
            SNAP_SPACER.wait()
            ret, df = self.q.get_market_snapshot(codes[i:i + 400])
            if not self.ok(ret):
                self._err(f"get_market_snapshot: {df}")
                self.caps["quote"] = False
                continue
            self.caps["quote"] = True
            out += rows(df)
        return out

    def expiries(self, code):
        CHAIN_SPACER.wait()
        ret, df = self.q.get_option_expiration_date(code=code)
        if not self.ok(ret):
            self._err(f"get_option_expiration_date {code}: {df}")
            return []
        return sorted({str(r.get("strike_time"))[:10] for r in rows(df) if r.get("strike_time")})

    def chain_codes(self, code, expiry):
        CHAIN_SPACER.wait()
        ret, df = self.q.get_option_chain(code=code, start=expiry, end=expiry)
        if not self.ok(ret):
            self._err(f"get_option_chain {code} {expiry}: {df}")
            self.caps["options"] = False
            return []
        self.caps["options"] = True
        return rows(df)

    def klines(self, code, days=1300):
        KLINE_SPACER.wait()
        mm = self.mm
        end = dt.date.today()
        start = end - dt.timedelta(days=int(days * 1.45))
        res = self.q.request_history_kline(code, start=str(start), end=str(end), ktype=mm.KLType.K_DAY, max_count=1000)
        ret, df = res[0], res[1]
        if not self.ok(ret):
            self._err(f"request_history_kline {code}: {df}")
            return []
        return [dict(d=str(r.get("time_key"))[:10], o=num(r.get("open")), h=num(r.get("high")), l=num(r.get("low")),
                     c=num(r.get("close")), v=num(r.get("volume"))) for r in rows(df)]

    # ---- account (read-only)
    def account(self, acc_id):
        mm = self.mm
        if self.t is None:
            return None
        out = dict(ts=utcnow(), funds=None, positions=[], deals=[])
        kw = dict(trd_env=mm.TrdEnv.REAL, acc_id=acc_id)
        try:
            ret, df = self.t.accinfo_query(currency=mm.Currency.USD, **kw)
            if self.ok(ret):
                r = rows(df)
                out["funds"] = {k: (num(v) if k != "currency" else str(v)) for k, v in (r[0] if r else {}).items()
                                if k in ("power", "total_assets", "cash", "market_val", "available_funds",
                                         "unrealized_pl", "realized_pl", "us_cash", "usd_net_cash_power", "currency")}
            else:
                self._err(f"accinfo_query: {df}")
            ret, df = self.t.position_list_query(**kw)
            if self.ok(ret):
                keep = ("code", "stock_name", "qty", "can_sell_qty", "cost_price", "cost_price_valid", "average_cost",
                        "diluted_cost", "market_val", "nominal_price", "pl_ratio", "pl_val", "unrealized_pl",
                        "realized_pl", "today_pl_val", "currency", "position_side")
                out["positions"] = [{k: r.get(k) for k in keep} for r in rows(df)]
                self.caps["account"] = True
            else:
                self.caps["account"] = False
                self._err(f"position_list_query: {df}")
            if hasattr(self.t, "history_deal_list_query"):
                end = dt.date.today()
                ret, df = self.t.history_deal_list_query(start=str(end - dt.timedelta(days=90)), end=str(end), **kw)
                if self.ok(ret):
                    out["deals"] = rows(df)[-200:]
        except Exception as ex:
            self._err(f"account sync: {ex}")
            self.caps["account"] = False
        return out

    # ---- orders (only tickets the user confirmed in SENTRY)
    def place(self, tk):
        mm = self.mm
        env = mm.TrdEnv.REAL if tk["env"] == "REAL" else mm.TrdEnv.SIMULATE
        if env == mm.TrdEnv.REAL:
            if not ENV["allow_real"]:
                return dict(ok=False, status="BLOCKED", message="MOOMOO_ALLOW_REAL_ORDERS is not 1 on the gateway")
            if not self.caps["trade_unlocked"]:
                if not ENV["trade_pwd"]:
                    return dict(ok=False, status="BLOCKED", message="MOOMOO_TRADE_PWD not set on the gateway")
                ret, msg = self.t.unlock_trade(ENV["trade_pwd"])
                if not self.ok(ret):
                    return dict(ok=False, status="REJECTED", message=f"unlock_trade failed: {msg}")
                self.caps["trade_unlocked"] = True
        side = mm.TrdSide.BUY if tk["side"] == "BUY" else mm.TrdSide.SELL
        ORDER_SPACER.wait()
        ret, df = self.t.place_order(price=float(tk["limit_price"]), qty=float(tk["qty"]), code=tk["code"],
                                     trd_side=side, order_type=mm.OrderType.NORMAL, trd_env=env,
                                     acc_id=ENV["acc_id"], remark=f"SENTRY#{tk['id']}")
        if not self.ok(ret):
            return dict(ok=False, status="REJECTED", message=str(df))
        r = rows(df)[0] if rows(df) else {}
        return dict(ok=True, status=str(r.get("order_status", "SUBMITTED")), order_id=str(r.get("order_id", "")),
                    message="Accepted by broker API - check fill status", raw={k: str(v) for k, v in r.items()})

    def order_status(self, order_id, env_name):
        mm = self.mm
        env = mm.TrdEnv.REAL if env_name == "REAL" else mm.TrdEnv.SIMULATE
        try:
            ret, df = self.t.order_list_query(order_id=order_id, trd_env=env, acc_id=ENV["acc_id"])
            if self.ok(ret) and len(df):
                r = rows(df)[0]
                return dict(status=str(r.get("order_status")), dealt_qty=num(r.get("dealt_qty")),
                            dealt_avg_price=num(r.get("dealt_avg_price")), message=str(r.get("last_err_msg", "")))
        except Exception as ex:
            self._err(f"order_list_query: {ex}")
        return None


# ------------------------------------------------------------------ cycle logic
def us(sym):
    return "US." + sym.upper().replace(".", "-")


def pick_expiries(exps, min_dte, max_dte, k):
    today = dt.date.today()
    inside = [e for e in exps if min_dte <= (dt.date.fromisoformat(e) - today).days <= max_dte]
    if len(inside) <= k:
        return inside
    idx = sorted({round(i * (len(inside) - 1) / (k - 1)) for i in range(k)})
    return [inside[i] for i in idx]


def chain_payload(od, sym, spot, expiry, band=0.4):
    contracts = od.chain_codes(us(sym), expiry)
    lo, hi = spot * (1 - band), spot * (1 + band)
    keep = [c for c in contracts if lo <= (num(c.get("strike_price")) or 0) <= hi]
    snaps = {s["code"]: s for s in od.snapshot([c["code"] for c in keep])} if keep else {}
    out = []
    for c in keep:
        s = snaps.get(c["code"], {})
        kind = "call" if "CALL" in str(c.get("option_type")).upper() else "put"
        out.append(dict(code=c["code"], kind=kind, strike=num(c.get("strike_price")), bid=num(s.get("bid_price")),
                        ask=num(s.get("ask_price")), last=num(s.get("last_price")), volume=num(s.get("volume")),
                        oi=num(s.get("option_open_interest")), iv=iv_frac(s.get("option_implied_volatility")),
                        delta=num(s.get("option_delta")), gamma=num(s.get("option_gamma")),
                        theta=num(s.get("option_theta")), vega=num(s.get("option_vega")), rho=num(s.get("option_rho")),
                        update_time=str(s.get("update_time", "")),
                        prev_close=num(s.get("prev_close_price"))))
    return out


class Cycle:
    def __init__(self, od, sentry):
        self.od, self.s = od, sentry
        self.rr, self.last_acct, self.kline_done = 0, 0.0, {}

    def run_once(self):
        work = self.s.call("GET", "/api/gateway/work") or {}
        tickers = [t.upper() for t in work.get("tickers", [])][: int(work.get("max_tickers", 25))]
        sync_sec = int(work.get("sync_sec", 60))

        if time.time() - self.last_acct >= sync_sec:
            acct = self.od.account(ENV["acc_id"])
            if acct is not None:
                self.s.call("POST", "/api/gateway/account", acct)
            self.last_acct = time.time()

        spots = {}
        if tickers:
            snaps = self.od.snapshot([us(t) for t in tickers])
            quotes = []
            for r in snaps:
                sym = str(r.get("code", ""))[3:]
                spots[sym] = num(r.get("last_price"))
                quotes.append(dict(ticker=sym, last=spots[sym], prev_close=num(r.get("prev_close_price")),
                                   bid=num(r.get("bid_price")), ask=num(r.get("ask_price")), volume=num(r.get("volume")),
                                   update_time=str(r.get("update_time", ""))))
            if quotes:
                self.s.call("POST", "/api/gateway/quotes", dict(ts=utcnow(), quotes=quotes))

        # option chains: a few tickers per cycle (round-robin) to respect the 10-per-30s chain limit
        per = int(work.get("chains_per_cycle", 3))
        win = work.get("window", {"min_dte": 7, "max_dte": 560})
        need = work.get("contracts", [])            # held contracts must always be refreshed
        order = [n["ticker"] for n in need] + tickers
        seen, batch = set(), []
        for t in order[self.rr:] + order[:self.rr]:
            if t not in seen and len(batch) < per:
                seen.add(t)
                batch.append(t)
        self.rr = (self.rr + per) % max(1, len(order))
        for sym in batch:
            spot = spots.get(sym)
            if not spot:
                snap = self.od.snapshot([us(sym)])
                spot = num(snap[0].get("last_price")) if snap else None
            if not spot:
                continue
            exps = self.od.expiries(us(sym))
            chosen = pick_expiries(exps, win["min_dte"], win["max_dte"], int(work.get("max_expiries", 4)))
            chosen = sorted(set(chosen) | {n["expiry"] for n in need if n["ticker"] == sym and n["expiry"] in exps})
            for e in chosen:
                contracts = chain_payload(self.od, sym, spot, e)
                if contracts:
                    self.s.call("POST", "/api/gateway/chain", dict(ts=utcnow(), ticker=sym, expiry=e, spot=spot,
                                                                   contracts=contracts))

        # daily candles once per day per ticker (moomoo's 7-day kline quota counts each ticker once)
        today = str(dt.date.today())
        for sym in work.get("kline_tickers", [])[:5]:
            if self.kline_done.get(sym) != today:
                bars = self.od.klines(us(sym))
                if bars:
                    self.s.call("POST", "/api/gateway/klines", dict(ts=utcnow(), ticker=sym, bars=bars))
                self.kline_done[sym] = today

        for tk in work.get("orders", []):
            res = self.od.place(tk)
            self.s.call("POST", "/api/gateway/order_result", dict(ticket_id=tk["id"], **res))
        for tk in work.get("order_status", []):
            st = self.od.order_status(tk["broker_order_id"], tk["env"])
            if st:
                self.s.call("POST", "/api/gateway/order_result", dict(ticket_id=tk["id"], ok=True, update=True, **st))

        caps = dict(self.od.caps, firm=ENV["firm"], real_orders_allowed_locally=ENV["allow_real"],
                    version="2026.10", host=ENV["host"], tickers=len(tickers), chain_batch=batch)
        self.s.call("POST", "/api/gateway/status", dict(ts=utcnow(), caps=caps))
        return work


def selftest():
    od = OpenD(ENV["host"], ENV["port"], ENV["firm"])
    od.connect()
    print("Connected to OpenD at", ENV["host"], ENV["port"])
    s = od.snapshot(["US.AAPL"])
    print("Stock snapshot:", "OK" if s else "FAILED", s[0].get("last_price") if s else "")
    exps = od.expiries("US.AAPL")
    print("Option expiries:", len(exps), exps[:3])
    if exps and s:
        c = chain_payload(od, "AAPL", num(s[0]["last_price"]), exps[min(3, len(exps) - 1)])
        priced = [x for x in c if x["bid"] and x["ask"]]
        print(f"Option chain: {len(c)} contracts, {len(priced)} with bid/ask (needs US options quote right)")
    a = od.account(ENV["acc_id"])
    print("Account:", "OK" if a and od.caps["account"] else "FAILED / no permission",
          f"- {len(a['positions'])} positions" if a else "")
    print("Errors:", od.caps["errors"] or "none")
    od.close()


def main():
    ap = argparse.ArgumentParser(description="SENTRY <- moomoo OpenD gateway")
    ap.add_argument("--selftest", action="store_true", help="check OpenD access and exit (sends nothing)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="read from OpenD but do not POST to SENTRY")
    ap.add_argument("--interval", type=int, default=20, help="seconds between cycles")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.selftest:
        return selftest()
    if not ENV["url"] or not ENV["token"]:
        sys.exit("Set SENTRY_URL and GATEWAY_TOKEN (same value as on the server).")
    od, s = OpenD(ENV["host"], ENV["port"], ENV["firm"]), Sentry(ENV["url"], ENV["token"], a.dry_run)
    cyc, backoff = Cycle(od, s), 5
    while True:
        try:
            if od.q is None:
                od.connect()
            cyc.run_once()
            backoff = 5
            if a.once:
                break
            time.sleep(a.interval)
        except KeyboardInterrupt:
            break
        except Exception as ex:          # OpenD restarted / logged out / network: reconnect with backoff
            log.exception("cycle failed: %s - reconnecting in %ss", ex, backoff)
            od.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
    od.close()


if __name__ == "__main__":
    main()
