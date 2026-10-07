"""A.I. Options Command Centre - web app (Render-ready).

Run locally:   python app.py            (http://localhost:8000)
Production:    gunicorn app:app --workers 1 --threads 8 --timeout 120
"""
import datetime as dt
import os
import sys

from flask import Flask, abort, jsonify, request, send_from_directory

# Works with BOTH repo layouts:
#   nested:  app.py + core/*.py + static/*      (as in the zip)
#   flat:    every file in the repo root         (what GitHub's web uploader produces)
HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "core")
sys.path.insert(0, HERE)
if os.path.isdir(CORE):
    sys.path.insert(0, CORE)
STATIC = os.path.join(HERE, "static") if os.path.isfile(os.path.join(HERE, "static", "app.js")) else HERE
STATIC_FILES = {"app.js", "style.css", "index.html"}

import logging                               # noqa: E402
import config, db, services, worker, auth    # noqa: E402
import moomoo_store as mms, orders           # noqa: E402
from market import market_status, overview   # noqa: E402
from flask import session                    # noqa: E402

logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","src":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("sentry")

from flask.json.provider import DefaultJSONProvider   # noqa: E402
import math as _math                                     # noqa: E402


def _clean(o):
    """Browsers reject NaN/Infinity in JSON - turn them into null everywhere."""
    if isinstance(o, float):
        return None if _math.isnan(o) or _math.isinf(o) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "item") and callable(o.item):          # numpy scalars
        try:
            return _clean(o.item())
        except Exception:
            return str(o)
    return o


class SafeJSON(DefaultJSONProvider):
    sort_keys = False

    def dumps(self, obj, **kw):
        kw.setdefault("allow_nan", False)
        return super().dumps(_clean(obj), **kw)


app = Flask(__name__, static_folder=None)
app.json = SafeJSON(app)
app.secret_key = auth.secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("RENDER") is not None, PERMANENT_SESSION_LIFETIME=14 * 86400)
app.before_request(auth.guard)


@app.errorhandler(Exception)
def on_error(ex):
    from werkzeug.exceptions import HTTPException
    if isinstance(ex, HTTPException):
        return ex
    log.exception("unhandled error on %s", request.path)
    return jsonify(error=f"Server error: {ex.__class__.__name__}"), 500
services.setup()
if config.RUN_WORKER:
    worker.start()


def body():
    return request.get_json(silent=True) or {}


def err(msg, code=400):
    return jsonify(error=msg), code


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<name>")
def static_file(name):
    if name not in STATIC_FILES:          # never serve source code or anything else from the folder
        abort(404)
    return send_from_directory(STATIC, name)


@app.get("/health")
def health():
    try:
        db.one("SELECT 1 AS ok")
        dbok = True
    except Exception:
        dbok = False
    return jsonify(status="ok" if dbok else "degraded", db=dbok, mode=config.DATA_MODE,
                   worker=worker.STATE["current"], time=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z")


# ---------------------------------------------------------------- login
@app.get("/api/me")
def api_me():
    return jsonify(login_required=auth.login_enabled(), logged_in=bool(session.get("auth")) or not auth.login_enabled())


@app.post("/api/login")
def api_login():
    if not auth.login_enabled():
        return jsonify(ok=True)
    ok_, why = auth.try_login(str(body().get("password", "")))
    return jsonify(ok=True) if ok_ else err(why, 401)


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


# ---------------------------------------------------------------- Moomoo gateway (signed, called by moomoo_gateway.py)
def _gw(fn):
    bad = auth.verify_gateway()
    if bad:
        return bad
    try:
        return jsonify(ok=True, result=fn(body()))
    except Exception as ex:
        log.exception("gateway push failed")
        return err(f"{ex.__class__.__name__}: {ex}")


@app.get("/api/gateway/work")
def gw_work():
    bad = auth.verify_gateway()
    return bad if bad else jsonify(mms.work())


@app.post("/api/gateway/status")
def gw_status():
    return _gw(mms.ingest_status)


@app.post("/api/gateway/account")
def gw_account():
    def f(b):
        n = mms.ingest_account(b)
        worker.run_now(services.evaluate_positions, "moomoo")
        return n
    return _gw(f)


@app.post("/api/gateway/quotes")
def gw_quotes():
    return _gw(mms.ingest_quotes)


@app.post("/api/gateway/chain")
def gw_chain():
    return _gw(mms.ingest_chain)


@app.post("/api/gateway/klines")
def gw_klines():
    return _gw(mms.ingest_klines)


@app.post("/api/gateway/order_result")
def gw_order_result():
    return _gw(mms.ingest_order_result)


# ---------------------------------------------------------------- Moomoo portfolio + orders (user side)
@app.get("/api/moomoo")
def api_moomoo():
    g = mms.gateway_info()
    acct = g.pop("account", None) or {}
    stocks = [p for p in acct.get("positions", []) if not mms.parse_code(p.get("code"))]
    return jsonify(gateway=g, funds=acct.get("funds"), account_ts=g.get("account_ts"), stocks=stocks,
                   options=services.position_rows("moomoo"), deals=(acct.get("deals") or [])[-50:],
                   closed=db.all("SELECT * FROM positions WHERE account='moomoo' AND status='CLOSED' ORDER BY id DESC LIMIT 50"),
                   real_orders=dict(server=config.ALLOW_REAL_ORDERS, setting=db.get_settings()["real_orders_enabled"]))


@app.get("/api/orders")
def api_orders():
    return jsonify(orders.listing())


@app.post("/api/orders/prepare")
def api_order_prepare():
    b = body()
    try:
        t = orders.prepare(b.get("env", "MOOMOO_PAPER"), b["ticker"], b["kind"], b["strike"], b["expiry"],
                           b.get("side", "BUY"), b.get("qty", 1), b["limit_price"], b.get("result_id"))
    except (KeyError, ValueError) as ex:
        return err(str(ex))
    return jsonify(t)


@app.post("/api/orders/<int:tid>/confirm")
def api_order_confirm(tid):
    try:
        return jsonify(orders.confirm(tid, body().get("phrase", "")))
    except ValueError as ex:
        return err(str(ex))


@app.post("/api/orders/<int:tid>/cancel")
def api_order_cancel(tid):
    return jsonify(ok=orders.cancel(tid))


# ---------------------------------------------------------------- options simulator
@app.post("/api/simulate")
def api_simulate():
    import optsim
    b = body()
    try:
        num = lambda k: float(b[k]) if b.get(k) not in (None, "") else None
        res = optsim.run(b["ticker"], b.get("kind", "call"), float(b["strike"]), b["expiry"], int(b.get("qty") or 1),
                         num("premium"), num("iv") / 100 if num("iv") else None, num("move_pct"),
                         int(b["hold_days"]) if b.get("hold_days") else None, b.get("iv_scenario", "IV unchanged"),
                         int(b.get("paths") or 20000))
    except (KeyError, ValueError) as ex:
        return err(str(ex))
    return jsonify(res)


@app.get("/api/chain")
def api_chain():
    """Expiries/strikes for the simulator pickers."""
    from providers import load_ticker
    t = (request.args.get("t") or "").upper()
    exp = request.args.get("exp")
    if not t:
        return err("ticker required")
    td = load_ticker(t, expiries=[exp] if exp else None, min_dte=1, max_dte=900, max_expiries=40, full=False)
    out = dict(ticker=t, price=td.price, chain_status=td.chain_status, expiries=sorted(td.chains))
    if exp and exp in td.chains:
        out["calls"] = td.chains[exp]["calls"][["strike", "bid", "ask"]].fillna(0).to_dict("records")
        out["puts"] = td.chains[exp]["puts"][["strike", "bid", "ask"]].fillna(0).to_dict("records")
    return jsonify(out)


# ---------------------------------------------------------------- backtesting
@app.get("/api/backtests")
def api_backtests():
    import backtest
    rows = db.all("SELECT id, ts, status, params FROM backtests ORDER BY id DESC LIMIT 20")
    for r in rows:
        r["params"] = db.jload(r["params"], {})
    return jsonify(runs=rows, defaults=backtest.DEFAULTS)


@app.post("/api/backtests")
def api_backtest_new():
    if db.one("SELECT id FROM backtests WHERE status='QUEUED' OR status LIKE 'RUNNING%'"):
        return err("A backtest is already running")
    bid = db.insert("backtests", dict(ts=services.iso(), status="QUEUED", params=db.jdump(body()), result=None))
    return jsonify(id=bid)


@app.get("/api/backtests/<int:bid>")
def api_backtest(bid):
    r = db.one("SELECT * FROM backtests WHERE id=?", (bid,))
    if not r:
        return err("not found", 404)
    r["params"], r["result"] = db.jload(r["params"], {}), db.jload(r["result"], None)
    return jsonify(r)


# ---------------------------------------------------------------- custom alert triggers
@app.get("/api/rules")
def api_rules():
    return jsonify(rules=db.all("SELECT * FROM alert_rules ORDER BY id DESC"), metrics=services.RULE_METRICS)


@app.post("/api/rules")
def api_rule_add():
    b = body()
    if b.get("metric") not in services.RULE_METRICS or b.get("op") not in (">=", "<="):
        return err("Pick a metric and >= or <=")
    try:
        rid = db.insert("alert_rules", dict(ts=services.iso(), ticker=str(b["ticker"]).upper()[:10],
                                            target=b.get("target") or "", metric=b["metric"], op=b["op"],
                                            value=float(b["value"]), note=str(b.get("note", ""))[:200], active=1))
    except (KeyError, ValueError) as ex:
        return err(f"Check the fields: {ex}")
    return jsonify(id=rid)


@app.delete("/api/rules/<int:rid>")
def api_rule_del(rid):
    db.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
    return jsonify(ok=True)


@app.post("/api/alerts/<int:aid>/ack")
def api_alert_ack(aid):
    services.ack_alert(aid)
    return jsonify(ok=True)


@app.post("/api/demo/reset")
def api_demo_reset():
    b = body()
    if str(b.get("confirm", "")).upper() != "RESET":
        return err('Type RESET to confirm')
    return jsonify(epoch=services.demo_reset(b.get("starting_cash") or None))


# ---------------------------------------------------------------- dashboard / market
@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(services.dashboard())


@app.get("/api/status")
def api_status():
    return jsonify(market=market_status(), data=services.data_status(), worker=worker.STATE)


@app.get("/api/market")
def api_market():
    macro, _ = services.macro_ctx()
    return jsonify(market=market_status(), indices=overview(),
                   macro=dict(score=macro["score"], label=macro["label"], notes=macro["notes"], flags=macro["flags"],
                              r=macro["r"], r_source=macro["r_source"], as_of=macro["as_of"]),
                   source=services._MACRO["meta"])


# ---------------------------------------------------------------- scanning
@app.post("/api/scan")
def api_scan():
    b = body()
    mode = (b.get("mode") or "GROWTH").upper()
    if mode not in config.MODES:
        return err("mode must be FAST, GROWTH or LEAPS")
    uni = [t.strip().upper() for t in (b.get("universe") or []) if t.strip()] or None
    running = db.one("SELECT id FROM scan_jobs WHERE status IN ('QUEUED','RUNNING')")
    if running:
        return jsonify(job_id=running["id"], note="A scan is already queued or running")
    return jsonify(job_id=services.create_scan(mode, uni))


@app.get("/api/jobs/latest")
def api_job_latest():
    return jsonify(db.one("SELECT * FROM scan_jobs ORDER BY id DESC LIMIT 1"))


@app.get("/api/jobs/<int:jid>")
def api_job(jid):
    return jsonify(db.one("SELECT * FROM scan_jobs WHERE id=?", (jid,)))


@app.get("/api/opportunities")
def api_opps():
    job, rows = services.latest_scan_rows(request.args.get("mode"))
    return jsonify(job=job, rows=rows, no_trade=bool(job) and not any(
        r.get("state") in ("BUY NOW", "BUY IF TRIGGERED") for r in rows))


@app.get("/api/report/<int:rid>")
def api_report(rid):
    r = db.one("SELECT payload FROM results WHERE id=?", (rid,))
    if not r or not r["payload"]:
        return err("report not found", 404)
    p = db.jload(r["payload"])
    p["result_id"] = rid
    return jsonify(enrich(p))


def enrich(p):
    """Add the portfolio-risk engine and calibration to a stored report (computed now, not stored)."""
    import risk
    if "contract" not in p:
        return p
    try:
        p["risk_engine"] = risk.check(p, deep=True)
    except Exception as ex:
        p["risk_engine"] = dict(ok=None, checks=[], error=str(ex))
    st = services.journal_stats()
    pop = (p.get("stats") or {}).get("p_profit")
    cal = next((c for c in st["calibration"] if pop is not None and c["predicted"] is not None and
                abs(c["predicted"] - pop) <= 0.05), None)
    if cal and cal["n"] >= 30:
        p["calibration"] = dict(status="CALIBRATED", text=f"Past recommendations with ~{cal['predicted']:.0%} predicted "
                                f"profit chance were actually profitable {cal['actual']:.0%} of the time ({cal['n']} cases).")
    else:
        p["calibration"] = dict(status="UNCALIBRATED", text="Not enough closed outcomes yet to check whether these "
                                "probabilities are honest - treat them as model estimates only.")
    return p


@app.get("/api/reports")
def api_reports():
    rows = db.all("SELECT id, ts, source, ticker, direction, mode, state, score, row FROM results "
                  "WHERE payload IS NOT NULL ORDER BY id DESC LIMIT 400")
    out, seen = [], set()
    for r in rows:
        k = (r["ticker"], r["direction"])
        if k in seen:
            continue
        seen.add(k)
        r["contract"] = (db.jload(r.pop("row"), {}) or {}).get("contract")
        out.append(r)
    return jsonify(out[:60])


# ---------------------------------------------------------------- watchlist
@app.get("/api/watchlist")
def api_watch():
    return jsonify(services.watchlist_rows())


@app.post("/api/watchlist")
def api_watch_add():
    t = (body().get("ticker") or "").upper().strip()
    if not t.replace("-", "").replace(".", "").isalnum() or len(t) > 10:
        return err("invalid ticker")
    try:
        wid = services.watch_add(t, body().get("mode"), body().get("list_name") or "Main")
    except ValueError as ex:
        return err(str(ex))
    if wid:
        worker.run_now(services.refresh_watch_item, db.one("SELECT * FROM watchlist WHERE id=?", (wid,)))
    return jsonify(ok=True, id=wid)


@app.delete("/api/watchlist/<int:wid>")
def api_watch_del(wid):
    db.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    return jsonify(ok=True)


@app.post("/api/watchlist/<int:wid>/favourite")
def api_watch_fav(wid):
    w = db.one("SELECT favourite FROM watchlist WHERE id=?", (wid,))
    db.update("watchlist", wid, dict(favourite=0 if w["favourite"] else 1))
    return jsonify(ok=True)


@app.post("/api/watchlist/refresh")
def api_watch_refresh():
    worker.run_now(services.refresh_watchlist)
    return jsonify(ok=True, note="Re-running the decision engine in the background")


# ---------------------------------------------------------------- real portfolio
@app.get("/api/portfolio")
def api_portfolio():
    return jsonify(summary=services.portfolio_summary(), positions=services.position_rows("real"),
                   closed=db.all("SELECT * FROM positions WHERE account='real' AND status='CLOSED' ORDER BY id DESC"))


@app.post("/api/positions")
def api_pos_add():
    b = body()
    try:
        kind = b["kind"].lower()
        assert kind in ("call", "put")
        dt.date.fromisoformat(b["expiry"])
        pid = services.add_position("real", b["ticker"], kind, float(b["strike"]), b["expiry"], float(b["qty"]),
                                    float(b["premium"]), b.get("entry_date") or None)
    except Exception as ex:
        return err(f"Check the fields: {ex}")
    worker.run_now(services.evaluate_positions, "real")
    return jsonify(id=pid)


@app.post("/api/positions/<int:pid>/close")
def api_pos_close(pid):
    try:
        services.close_position(pid, float(body()["exit_premium"]), body().get("reason") or "Manual close")
    except Exception as ex:
        return err(str(ex))
    return jsonify(ok=True)


@app.delete("/api/positions/<int:pid>")
def api_pos_del(pid):
    p = db.one("SELECT account FROM positions WHERE id=?", (pid,))
    if not p or p["account"] != "real":
        return err("Only real positions can be deleted (demo history is never rewritten)")
    db.execute("DELETE FROM positions WHERE id=?", (pid,))
    return jsonify(ok=True)


@app.post("/api/positions/refresh")
def api_pos_refresh():
    worker.run_now(services.evaluate_positions)
    return jsonify(ok=True)


@app.get("/api/positions/<int:pid>/report")
def api_pos_report(pid):
    p = db.one("SELECT * FROM positions WHERE id=?", (pid,))
    if not p:
        return err("not found", 404)
    ev = db.jload(p["last_eval"], {})
    rep = ev.pop("report", None)
    if not rep:
        return err("Not evaluated yet - try again in a minute", 404)
    rep["position"] = dict(ev, entry_premium=p["entry_premium"], qty=p["qty"], account=p["account"], id=pid,
                           broker_code=p.get("broker_code"))
    return jsonify(enrich(rep))


# ---------------------------------------------------------------- demo account
@app.get("/api/demo")
def api_demo():
    return jsonify(metrics=services.demo_metrics(), positions=services.position_rows("demo"),
                   trades=db.all("SELECT * FROM demo_trades ORDER BY id DESC LIMIT 200"),
                   auto_log=db.all("SELECT * FROM auto_log ORDER BY id DESC LIMIT 100"))


@app.post("/api/demo/buy")
def api_demo_buy():
    b = body()
    rec = None
    if b.get("result_id"):
        r = db.one("SELECT payload FROM results WHERE id=?", (int(b["result_id"]),))
        rec = db.jload(r["payload"]) if r else None
        if not rec:
            return err("recommendation not found")
        c = rec["contract"]
        if len(c["legs"]) != 1:
            return err("Paper trading supports single-leg contracts")
        t, kind, strike, exp = rec["ticker"], c["kind"], c["legs"][0]["K"], c["expiry"]
    else:
        t, kind, strike, exp = b.get("ticker", ""), b.get("kind", "call"), float(b.get("strike", 0)), b.get("expiry", "")
    pid, e = services.paper_buy(t, kind, strike, exp, int(b.get("qty", 1)), b.get("reason") or "Manual paper buy", rec=rec)
    if e:
        return err(e)
    worker.run_now(services.evaluate_positions, "demo")
    return jsonify(position_id=pid)


@app.post("/api/demo/sell")
def api_demo_sell():
    b = body()
    pnl, e = services.paper_sell(int(b["position_id"]), b.get("qty"), b.get("reason") or "Manual paper sell")
    return err(e) if e else jsonify(pnl=pnl)


# ---------------------------------------------------------------- alerts / actions / journal
@app.get("/api/alerts")
def api_alerts():
    return jsonify(alerts=db.all("SELECT * FROM alerts ORDER BY id DESC LIMIT 300"), types=config.ALERT_TYPES,
                   enabled=db.get_settings()["alerts"])


@app.post("/api/alerts/read")
def api_alerts_read():
    db.execute("UPDATE alerts SET is_read=1")
    return jsonify(ok=True)


@app.get("/api/actions")
def api_actions():
    return jsonify(services.action_queue())


@app.get("/api/journal")
def api_journal():
    rows = db.all("SELECT id, ts, source, ticker, direction, mode, strategy, contract, state, score, pop, ev, entry_ask, "
                  "trigger_upper, time_exit, regime, outcome_status, outcome_return, outcome_note FROM journal "
                  "ORDER BY id DESC LIMIT 300")
    for r in rows:
        r["contract"] = (db.jload(r["contract"], {}) or {}).get("name")
    return jsonify(stats=services.journal_stats(), entries=rows)


# ---------------------------------------------------------------- diagnostics
@app.get("/api/diagnostics")
def api_diagnostics():
    """Tests every data source live and reports exactly what works - no caching."""
    import time as _t
    import providers as pv
    sym = (request.args.get("t") or "SPY").upper()[:10]
    pv.CACHE._d.clear()
    P = pv.providers()
    checks = []

    def run(name, fn):
        t0 = _t.time()
        try:
            ok, detail = fn()
        except Exception as ex:
            ok, detail = False, f"{ex.__class__.__name__}: {str(ex)[:160]}"
        checks.append(dict(name=name, ok=bool(ok), detail=f"{detail} ({_t.time() - t0:.1f}s)"))

    if P.mode == "demo":
        return jsonify(checks=[dict(name="Data mode", ok=False, detail="DATA_MODE=demo: synthetic data, no live connections used")],
                       summary="Set DATA_MODE=live to use real data.")
    try:
        import yfinance
        yv = yfinance.__version__
    except Exception as ex:
        yv = f"not importable: {ex}"
    checks.append(dict(name="yfinance version", ok=not yv.startswith("not"), detail=yv))
    run("Yahoo price history", lambda: (lambda h, m: (h is not None, f"{len(h)} bars" if h is not None else m["note"]))(*P.stock.history(sym)))
    run("Yahoo quote", lambda: (lambda q, m: (q is not None, f"{q[0]:.2f}" if q else m["note"]))(*P.stock.quote(sym)))
    run("Yahoo option expiries", lambda: (lambda e, m: (bool(e), f"{len(e)} expiries" if e else m["note"]))(*pv.Yahoo().expiries(sym)))
    run("Cboe option chain", lambda: (lambda e, m: (bool(e), f"{len(e)} expiries, {m['freshness']}" if e else m["note"]))(*P.cboe.expiries(sym)))
    run("Option source used by the app", lambda: (lambda e, m: (bool(e), m["source"] if e else m["note"]))(*P.options.expiries(sym)))
    run("SEC EDGAR fundamentals", lambda: (lambda f, m: (bool(f) or sym in pv.KNOWN_ETFS,
                                                         "ETF - no company filings" if sym in pv.KNOWN_ETFS else (f"{len(f)} fields" if f else m["note"])))(*P.fundamentals.fundamentals(sym)))
    run("FRED macro", lambda: (lambda s_, m: (bool(s_), f"{len(s_)} series" if s_ else m["note"]))(*P.macro.series()))
    run("Finnhub (optional)", lambda: (lambda q, m: (q is not None, f"{q}" if q else m["note"]))(*P.news.quote(sym)))
    g = mms.gateway_info()
    checks.append(dict(name="Moomoo gateway (optional)", ok=g["connected"],
                       detail=("online, last push " + str(g["last_seen"])) if g["connected"] else
                       ("token set, gateway not seen" if g["configured"] else "GATEWAY_TOKEN not set")))
    bad = [c["name"] for c in checks if not c["ok"] and "optional" not in c["name"]]
    summary = "All required sources OK." if not bad else "Problems: " + ", ".join(bad) + \
        ". Option scans need either 'Cboe option chain' or 'Yahoo option expiries' to pass."
    return jsonify(checks=checks, summary=summary)


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def api_settings():
    from providers import providers
    return jsonify(settings=db.get_settings(), modes=config.MODES, alert_types=config.ALERT_TYPES,
                   security=dict(login=auth.login_enabled(), gateway_token=bool(config.GATEWAY_TOKEN),
                                 allow_real_orders=config.ALLOW_REAL_ORDERS, webhook=bool(config.ALERT_WEBHOOK_URL),
                                 secret_key=bool(config.SECRET_KEY)),
                   gateway=mms.gateway_info() | {"account": None},
                   providers=providers().describe(), database="PostgreSQL" if db.PG else "SQLite")


@app.post("/api/settings")
def api_settings_save():
    return jsonify(settings=db.save_settings(body()))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
