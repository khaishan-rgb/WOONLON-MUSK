"""Phase 3 - manual-approved order tickets (master spec section 12).

Nothing here trades on its own. The flow is:
  1. DRAFT      - you press "Prepare order" on a report. We show the exact contract, side, quantity, limit price,
                  total cost incl. estimated fees, maximum loss, and every pre-trade check.
  2. CONFIRMED  - you type the confirmation phrase. Allowed only if every hard check passes.
  3. SENT       - the gateway on your machine picks it up within ~20 s (tickets older than 5 min expire).
  4. SUBMITTED / REJECTED / FILLED - only what the broker API returns. We never assume success.

Environments:
  MOOMOO_PAPER  Moomoo's own paper-trading account (TrdEnv.SIMULATE) - works without a trade password.
  REAL          Real money. Needs ALL of: ALLOW_REAL_ORDERS=1 on the server, "real orders" switched on in Settings,
                MOOMOO_ALLOW_REAL_ORDERS=1 and MOOMOO_TRADE_PWD on the gateway machine. Off by default.
"""
import datetime as dt

import config
import db
import moomoo_store as mms

ENVS = ("MOOMOO_PAPER", "REAL")
FINAL = ("FILLED", "REJECTED", "BLOCKED", "EXPIRED", "CANCELLED")


def iso():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def occ_code(ticker, expiry, kind, strike):
    d = dt.date.fromisoformat(expiry)
    return f"US.{ticker.upper()}{d:%y%m%d}{'C' if kind == 'call' else 'P'}{int(round(float(strike) * 1000))}"


def _checks(t):
    s = db.get_settings()
    g = mms.gateway_info()
    c = []
    add = lambda n, ok, d, hard=True: c.append(dict(name=n, passed=bool(ok), detail=d, hard=hard))
    add("Moomoo gateway connected", g["connected"], f"last seen {g.get('last_seen') or 'never'}")
    if t["env"] == "REAL":
        add("Real orders allowed on server (ALLOW_REAL_ORDERS=1)", config.ALLOW_REAL_ORDERS, "environment variable")
        add("Real orders switched on in Settings", s["real_orders_enabled"], "Settings -> API connections")
        add("Gateway allows real orders", (g.get("caps") or {}).get("real_orders_allowed_locally"),
            "MOOMOO_ALLOW_REAL_ORDERS=1 on your computer")
        funds, ts = mms.latest_funds()
        if t["side"] == "BUY":
            add("Buying power", funds and funds.get("power") is not None and float(funds["power"]) >= t["est_cost"],
                f"${float(funds['power']):,.2f} vs ${t['est_cost']:,.2f}" if funds and funds.get("power") is not None
                else "unknown - account not synced")
        if t["side"] == "SELL":
            held = db.one("SELECT qty FROM positions WHERE account='moomoo' AND status='OPEN' AND broker_code=?", (t["code"],))
            add("You hold enough contracts to sell", held and held["qty"] >= t["qty"],
                f"held {held['qty'] if held else 0:g}, selling {t['qty']:g} (never opens a short position)")
        add("Account permissions", (g.get("caps") or {}).get("account"), "position query succeeded on the gateway")
    add("Quantity 1-50 contracts", 1 <= t["qty"] <= 50, f"{t['qty']:g}")
    add("Limit price set", t["limit_price"] > 0, f"${t['limit_price']:.2f}")
    exp_ok = dt.date.fromisoformat(t["expiry"]) > dt.date.today()
    add("Contract not expired", exp_ok, t["expiry"])
    if t["side"] == "BUY" and t.get("result_id"):
        r = db.one("SELECT state, payload FROM results WHERE id=?", (t["result_id"],))
        if r:
            add("AI verdict supports buying", r["state"] in ("BUY NOW", "BUY IF TRIGGERED"),
                f"AI says {r['state']} - you can still proceed, it is your decision", hard=False)
            try:
                import risk
                chk = risk.check(db.jload(r["payload"]), contracts=int(t["qty"]), deep=False)
                bad = [x["name"] for x in chk["checks"] if not x["passed"] and x["severity"] == "SEVERE"]
                add("Portfolio risk limits", chk["ok"], "; ".join(bad) or "within your limits", hard=False)
            except Exception:
                pass
    return c


def prepare(env, ticker, kind, strike, expiry, side, qty, limit_price, result_id=None):
    if env not in ENVS:
        raise ValueError("env must be MOOMOO_PAPER or REAL")
    side, kind = side.upper(), kind.lower()
    if side not in ("BUY", "SELL") or kind not in ("call", "put"):
        raise ValueError("Bad side or option type")
    qty, limit_price = float(int(float(qty))), round(float(limit_price), 2)
    fee = float(db.get_settings()["fee_per_contract"])
    est = limit_price * 100 * qty + fee * qty
    t = dict(env=env, ticker=ticker.upper(), code=occ_code(ticker, expiry, kind, strike), kind=kind, strike=float(strike),
             expiry=expiry, side=side, qty=qty, limit_price=limit_price, est_cost=round(est, 2),
             max_loss=round(est, 2) if side == "BUY" else 0.0, result_id=result_id)
    checks = _checks(t)
    t.update(ts=iso(), status="DRAFT", checks=db.jdump(checks),
             note="Limit order, day only. Buying an option can lose the whole premium.")
    t["id"] = db.insert("order_tickets", t)
    t["checks"] = checks
    return t


def confirm(tid, phrase):
    t = db.one("SELECT * FROM order_tickets WHERE id=?", (tid,))
    if not t or t["status"] != "DRAFT":
        raise ValueError("Ticket is not a draft")
    if mms.age_sec(t["ts"]) > 600:
        db.update("order_tickets", tid, dict(status="EXPIRED", note="Draft older than 10 minutes - prepare again"))
        raise ValueError("Draft expired (prices move) - prepare a new ticket")
    want = f"{t['side']} {int(t['qty'])} {t['ticker']}"
    if (phrase or "").strip().upper() != want:
        raise ValueError(f'Type exactly: {want}')
    checks = _checks(t)
    if not all(c["passed"] for c in checks if c.get("hard", True)):
        db.update("order_tickets", tid, dict(checks=db.jdump(checks)))
        raise ValueError("Checks failed: " + "; ".join(c["name"] for c in checks if not c["passed"] and c.get("hard", True)))
    db.update("order_tickets", tid, dict(status="CONFIRMED", confirm_ts=iso(), checks=db.jdump(checks)))
    return db.one("SELECT * FROM order_tickets WHERE id=?", (tid,))


def cancel(tid):
    t = db.one("SELECT status FROM order_tickets WHERE id=?", (tid,))
    if t and t["status"] in ("DRAFT", "CONFIRMED"):
        db.update("order_tickets", tid, dict(status="CANCELLED", note="Cancelled in SENTRY before sending"))
        return True
    return False


def listing():
    out = db.all("SELECT * FROM order_tickets ORDER BY id DESC LIMIT 100")
    for r in out:
        r["checks"] = db.jload(r["checks"], [])
        r["broker_response"] = db.jload(r["broker_response"], None)
    return out
