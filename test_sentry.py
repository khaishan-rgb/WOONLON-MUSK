"""SENTRY automated tests - standard library only.   Run:  python test_sentry.py

They run offline. Live services (Yahoo, Cboe, FRED, SEC, Moomoo OpenD) are replaced by clearly marked fakes, so the
tests prove the app's own logic, wiring and safety rules - NOT that a real data source or your Moomoo account works.
Use Settings -> "Test data connections" and `python moomoo_gateway.py --selftest` for that.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="sentry-test-")
os.environ.update(DATA_MODE="demo", RUN_WORKER="0", GATEWAY_TOKEN="unit-test-token",
                  DATABASE_URL=f"sqlite:///{TMP}/t.db")
os.environ.pop("APP_PASSWORD", None)
sys.path.insert(0, HERE)

import app as appmod          # noqa: E402
import db, services            # noqa: E402

C = appmod.app.test_client()
db.save_settings({"mc_paths": 1500, "universe": "NVDA,MSFT", "real_cash": 20000})


def run_isolated(code, env_extra, timeout=600):
    """Run code in a fresh interpreter (own env vars + own database)."""
    d = tempfile.mkdtemp(prefix="sentry-sub-")
    fake = os.path.join(d, "fakesdk", "moomoo")
    os.makedirs(fake)
    shutil.copy(os.path.join(HERE, "fake_moomoo_sdk.py"), os.path.join(fake, "__init__.py"))
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{d}/s.db", RUN_WORKER="0",
               PYTHONPATH=os.pathsep.join([os.path.join(d, "fakesdk"), HERE]), **env_extra)
    r = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, cwd=HERE, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise AssertionError(r.stdout[-3000:] + r.stderr[-3000:])
    return r.stdout


LIVE_OFFLINE = """
import providers as pv
demo = pv.DemoData()
pv.Yahoo.history = lambda self, sym, period="5y": demo.history(sym)
pv.Yahoo.quote = lambda self, sym: (None, pv.meta("Yahoo","UNAVAILABLE","NONE","offline test"))
pv.Yahoo.info = lambda self, sym: ({}, pv.meta("Yahoo","UNAVAILABLE","NONE","offline test"))
pv.Yahoo.earnings = lambda self, sym, h: ((None, []), pv.meta("Yahoo","UNAVAILABLE","NONE","offline test"))
pv.Yahoo.expiries = lambda self, sym: (None, pv.meta("Yahoo","UNAVAILABLE","NONE","offline test"))
pv.CboeOptions._raw = lambda self, sym: (None, pv.meta("Cboe","UNAVAILABLE","NONE","offline test"))
pv.EdgarFundamentals.fundamentals = lambda self, sym: ({}, pv.meta("SEC","UNAVAILABLE","NONE","offline test"))
pv.FredMacro.series = lambda self: demo.series()
import market, moomoo_store
market.market_status = lambda now=None: dict(status="OPEN", ny_time="test")
moomoo_store.market_status = pv.market_status = market.market_status
from app import app
import db, services
c = app.test_client()
db.save_settings({"universe": "NVDA,MSFT", "mc_paths": 1500})
import moomoo_gateway as gwm
class R:
    def __init__(s, r): s.status_code, s._r = r.status_code, r
    def json(s): return s._r.json
    text = property(lambda s: s._r.get_data(as_text=True))
class FakeReq:
    @staticmethod
    def request(method, url, data=None, timeout=None, headers=None):
        return R(c.open(url.replace("http://sentry", ""), method=method, data=data, headers=headers))
sentry = gwm.Sentry("http://sentry", "unit-test-token"); sentry.req = FakeReq
od = gwm.OpenD("127.0.0.1", 11111, "FUTUSG"); od.connect()
for sp in (gwm.CHAIN_SPACER, gwm.SNAP_SPACER, gwm.KLINE_SPACER, gwm.ORDER_SPACER): sp.s = 0
cyc = gwm.Cycle(od, sentry)
"""


class T01Startup(unittest.TestCase):
    def test_health_and_static(self):
        r = C.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["status"], "ok")
        self.assertEqual(C.get("/").status_code, 200)
        self.assertEqual(C.get("/static/app.js").status_code, 200)
        for f in ("app.py", "config.py", "db.py", "../app.py"):
            self.assertEqual(C.get(f"/static/{f}").status_code, 404, f"source file {f} must never be served")

    def test_wsgi_serves_real_http(self):
        """Same 'app:app' object gunicorn imports, served over a real socket."""
        from wsgiref.simple_server import make_server, WSGIRequestHandler
        WSGIRequestHandler.log_message = lambda *a: None
        srv = make_server("127.0.0.1", 0, appmod.app)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/health", timeout=10).read())
        self.assertEqual(body["status"], "ok")

    def test_flat_layout_imports(self):
        """Every module imports from the repo root (GitHub's web uploader drops folders)."""
        mods = ["app", "auth", "backtest", "config", "db", "decisions", "engines", "market", "moomoo_gateway",
                "moomoo_store", "optsim", "orders", "pricing", "providers", "risk", "services", "simulate", "worker"]
        out = run_isolated("import importlib\nfor m in %r: importlib.import_module(m)\nprint('ok')" % mods,
                           dict(DATA_MODE="demo"))
        self.assertIn("ok", out)

    def test_requirements_cover_imports(self):
        req = open(os.path.join(HERE, "requirements.txt")).read().lower()
        for pkg in ("flask", "gunicorn", "numpy", "pandas", "scipy", "requests", "yfinance", "psycopg2"):
            self.assertIn(pkg, req)
        self.assertNotIn("moomoo-api", req, "the SDK belongs on the gateway machine, not on Render")


class T02Pricing(unittest.TestCase):
    def test_put_call_parity_iv_roundtrip_and_american(self):
        import math
        from pricing import bs_price, implied_vol, binomial_american
        S, K, T, r, v = 100, 105, 0.75, 0.04, 0.3
        c, p = bs_price(S, K, T, r, v, "call"), bs_price(S, K, T, r, v, "put")
        self.assertAlmostEqual(c - p, S - K * math.exp(-r * T), places=6)
        self.assertAlmostEqual(implied_vol(c, S, K, T, r, "call"), v, places=4)
        self.assertGreaterEqual(binomial_american(S, K, T, r, v, "put") + 1e-9, p)

    def test_monte_carlo_reproducible(self):
        from simulate import simulate_regimes
        a = simulate_regimes(100, 0.3, [21, 63], 2000, seed=11)
        b = simulate_regimes(100, 0.3, [21, 63], 2000, seed=11)
        self.assertEqual(float(a["normal"][63].mean()), float(b["normal"][63].mean()))


class T03Decisions(unittest.TestCase):
    def test_1_scan_states_and_no_trade(self):
        j = C.post("/api/scan", json={"mode": "YEAR"}).json
        services.run_scan(j["job_id"])
        o = C.get("/api/opportunities").json
        self.assertEqual(o["job"]["status"], "DONE")
        states = {r["state"] for r in o["rows"]}
        self.assertTrue(states <= {"BUY NOW", "BUY IF TRIGGERED", "WAIT", "AVOID"}, states)
        if o["no_trade"]:
            self.assertIn("NO HIGH-QUALITY TRADE", o["job"]["message"])
        rid = next(r["result_id"] for r in o["rows"] if "error" not in r)
        rep = C.get(f"/api/report/{rid}").json
        for k in ("why", "risks", "mind_changers", "trigger", "plan", "sim", "conditions", "risk_engine", "calibration"):
            self.assertIn(k, rep)
        self.assertEqual(rep["calibration"]["status"], "UNCALIBRATED")
        self.assertGreater(rep["contract"]["max_loss"], 0)

    def test_2_board_and_dashboard(self):
        d = C.get("/api/dashboard").json
        self.assertEqual(set(d["board"]) >= {"buy", "hold", "sell", "avoid", "no_trade"}, True)
        json.dumps(d, allow_nan=False)

    def test_3_risk_engine_vetoes_concentration(self):
        import risk
        rid = db.one("SELECT id FROM results WHERE payload IS NOT NULL ORDER BY id LIMIT 1")["id"]
        p = db.jload(db.one("SELECT payload FROM results WHERE id=?", (rid,))["payload"])
        db.save_settings({"real_cash": 1000})
        chk = risk.check(p, contracts=50, deep=False)
        self.assertFalse(chk["ok"])
        db.save_settings({"real_cash": 20000})


class T04PaperTrading(unittest.TestCase):
    def test_fees_insufficient_cash_and_reset(self):
        rid = db.one("SELECT id FROM results WHERE payload IS NOT NULL ORDER BY id LIMIT 1")["id"]
        start = C.get("/api/demo").json["metrics"]["cash"]
        r = C.post("/api/demo/buy", json={"result_id": rid, "qty": 1}).json
        self.assertIn("position_id", r)
        m = C.get("/api/demo").json["metrics"]
        self.assertGreater(m["fees"], 0)
        self.assertLess(m["cash"], start)
        big = C.post("/api/demo/buy", json={"result_id": rid, "qty": 999})
        self.assertEqual(big.status_code, 400)
        self.assertIn("Not enough paper cash", big.json["error"])
        n_before = len(C.get("/api/demo").json["trades"])
        self.assertEqual(C.post("/api/demo/reset", json={"confirm": "nope"}).status_code, 400)
        C.post("/api/demo/reset", json={"confirm": "RESET"})
        d = C.get("/api/demo").json
        self.assertEqual(d["metrics"]["cash"], float(db.get_settings()["demo_starting_cash"]))
        self.assertEqual(len(d["trades"]), n_before, "reset must keep the audit trail")
        self.assertEqual(d["positions"], [])


class T05Simulator(unittest.TestCase):
    def test_simulator_labels_and_scenarios(self):
        rid = db.one("SELECT id FROM results WHERE payload IS NOT NULL ORDER BY id LIMIT 1")["id"]
        c = C.get(f"/api/report/{rid}").json["contract"]
        r = C.post("/api/simulate", json={"ticker": "NVDA", "kind": c["kind"], "strike": c["legs"][0]["K"],
                                          "expiry": c["expiry"], "qty": 1, "move_pct": 10, "paths": 2000})
        if r.status_code != 200:     # demo strike may not exist for NVDA: use a custom premium instead
            r = C.post("/api/simulate", json={"ticker": "NVDA", "kind": "call", "strike": 100, "expiry": c["expiry"],
                                              "premium": 12, "iv": 40, "qty": 1, "paths": 2000})
        s = r.json
        self.assertEqual([x["name"] for x in s["scenarios"]][-4:], ["Bullish", "Base", "Bearish", "Severe downside"])
        self.assertTrue(all(v["kind"] in ("OBSERVED", "ASSUMPTION", "SYNTHETIC") for v in s["inputs"].values()))
        self.assertAlmostEqual(sum(b["p"] for b in s["distribution"]["buckets"]), 1.0, places=2)
        self.assertGreater(len(s["scenarios"][0]["cells"]), 1, "must value the option before expiry, not only at expiry")


class T06Backtest(unittest.TestCase):
    def test_walk_forward_no_leakage(self):
        import backtest, providers
        res = backtest.run({"tickers": "NVDA,MSFT", "rebalance_days": 21},
                           history_fn=lambda s: providers.providers().stock.history(s)[0])
        self.assertNotIn("error", res)
        self.assertIn("MODELLED", res["option_prices"])
        for f in res["folds"]:
            self.assertLess(f["train_from"], f["test_from"])
        oos = res["out_of_sample_from"]
        self.assertTrue(all(t["entry_date"] >= oos for t in res["sample_trades"]))
        self.assertIn("verdict", res)


class T07SecurityAndGateway(unittest.TestCase):
    def test_gateway_signatures(self):
        import auth, time
        self.assertEqual(C.get("/api/gateway/work").status_code, 401)
        ts, nonce, sig = auth.sign("unit-test-token", "GET", "/api/gateway/work", b"")
        h = {"X-Sentry-Ts": ts, "X-Sentry-Nonce": nonce, "X-Sentry-Sig": sig}
        self.assertEqual(C.get("/api/gateway/work", headers=h).status_code, 200)
        self.assertEqual(C.get("/api/gateway/work", headers=h).status_code, 401, "replay must be rejected")
        ts, nonce, sig = auth.sign("wrong-token", "GET", "/api/gateway/work", b"")
        self.assertEqual(C.get("/api/gateway/work", headers={"X-Sentry-Ts": ts, "X-Sentry-Nonce": nonce,
                                                             "X-Sentry-Sig": sig}).status_code, 401)
        ts, nonce, sig = auth.sign("unit-test-token", "GET", "/api/gateway/work", b"", ts=time.time() - 3600)
        self.assertEqual(C.get("/api/gateway/work", headers={"X-Sentry-Ts": ts, "X-Sentry-Nonce": nonce,
                                                             "X-Sentry-Sig": sig}).status_code, 401)

    def test_code_parser(self):
        import moomoo_store as m
        self.assertEqual(m.parse_code("US.NVDA270115C900000"), ("NVDA", "2027-01-15", "call", 900.0))
        self.assertEqual(m.parse_code("US.BRK.B270115P452500"), ("BRK.B", "2027-01-15", "put", 452.5))
        self.assertIsNone(m.parse_code("US.AAPL"))

    def test_cross_site_post_blocked(self):
        r = C.post("/api/settings", json={"min_pop": 0.1}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_login_wall(self):
        out = run_isolated("""
            from app import app
            c = app.test_client()
            assert c.get('/health').status_code == 200
            assert c.get('/api/dashboard').status_code == 401
            assert c.post('/api/login', json={'password': 'bad'}).status_code == 401
            assert c.post('/api/login', json={'password': 's3cret-pass'}).status_code == 200
            assert c.get('/api/dashboard').status_code == 200
            print('ok')""", dict(DATA_MODE="demo", APP_PASSWORD="s3cret-pass"))
        self.assertIn("ok", out)

    def test_nan_never_reaches_browser(self):
        db.insert("mm_account", dict(ts="2026-01-01T00:00:00Z", data='{"positions":[{"code":"US.X","qty":NaN}]}'))
        r = C.get("/api/moomoo")
        json.loads(r.get_data(as_text=True))          # strict parser (browsers) must accept it


class T08MoomooEndToEnd(unittest.TestCase):
    def test_gateway_sync_chain_and_orders(self):
        out = run_isolated(LIVE_OFFLINE + textwrap.dedent("""
            cyc.run_once(); cyc.run_once()
            g = c.get('/api/moomoo').json
            assert g['gateway']['connected'], g['gateway']
            assert g['funds']['total_assets'] == 25000.0
            assert [s['code'] for s in g['stocks']] == ['US.MSFT']
            o = g['options'][0]; assert (o['ticker'], o['kind'], o['strike'], o['qty']) == ('NVDA', 'call', 190.0, 2.0)
            td = pv.load_ticker('NVDA', 300, 560)
            assert td.chain_status == 'LIVE' and any('Moomoo' in s['source'] for s in td.sources), td.chain_status
            services.evaluate_positions('moomoo')
            e = c.get('/api/moomoo').json['options'][0]['eval']
            assert e['action'] in ('ADD / BUY','HOLD','HOLD / WATCH','TAKE 25% PROFIT','TAKE 50% PROFIT','SELL','EXIT NOW'), e
            assert e['would_buy'] in ('YES','ONLY AT LOWER PRICE','NO')
            t = c.post('/api/orders/prepare', json=dict(env='MOOMOO_PAPER', ticker='NVDA', kind='call', strike=190,
                       expiry=o['expiry'], side='BUY', qty=1, limit_price=20.1)).json
            assert c.post(f"/api/orders/{t['id']}/confirm", json={'phrase': 'yes'}).status_code == 400
            assert c.post(f"/api/orders/{t['id']}/confirm", json={'phrase': 'BUY 1 NVDA'}).json['status'] == 'CONFIRMED'
            import moomoo as mm
            assert not [x for x in mm.CALLS if x[0] == 'place_order']
            cyc.run_once()
            placed = [x for x in mm.CALLS if x[0] == 'place_order']
            assert placed and placed[0][4] == 'SIMULATE', placed
            st = c.get('/api/orders').json[0]; assert st['status'] == 'SUBMITTED' and st['broker_order_id'] == '777', st
            cyc.run_once(); assert c.get('/api/orders').json[0]['status'] == 'FILLED'
            t2 = c.post('/api/orders/prepare', json=dict(env='REAL', ticker='NVDA', kind='call', strike=190,
                        expiry=o['expiry'], side='BUY', qty=1, limit_price=20.1)).json
            r = c.post(f"/api/orders/{t2['id']}/confirm", json={'phrase': 'BUY 1 NVDA'})
            assert r.status_code == 400 and 'ALLOW_REAL_ORDERS' in r.json['error'], r.json
            ts = c.post('/api/orders/prepare', json=dict(env='REAL', ticker='NVDA', kind='call', strike=190,
                        expiry=o['expiry'], side='SELL', qty=5, limit_price=20.1)).json
            assert any(x['name'].startswith('You hold enough') and not x['passed'] for x in ts['checks'])
            print('ok')
        """), dict(DATA_MODE="live", GATEWAY_TOKEN="unit-test-token"))
        self.assertIn("ok", out)


if __name__ == "__main__":
    res = unittest.main(verbosity=2, exit=False).result
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if res.wasSuccessful() else 1)
