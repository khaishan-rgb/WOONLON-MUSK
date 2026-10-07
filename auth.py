"""Security: app login and signed requests from the Moomoo gateway.

* App login - set APP_PASSWORD on the server. Every page and /api call then needs a signed session cookie.
  /health stays open for Render's health check. Without APP_PASSWORD the app is open and shows a warning.
* Gateway - moomoo_gateway.py signs every request with HMAC-SHA256 using GATEWAY_TOKEN (never sent over
  the wire). Requests older than 5 minutes or re-used nonces are rejected (replay protection).
No secret is ever stored in the database or sent to the browser.
"""
import hashlib
import hmac
import os
import threading
import time
from collections import deque

from flask import jsonify, request, session

import config

SIG_WINDOW = 300
_nonces, _nonce_lock = {}, threading.Lock()
_fails, _fail_lock = {}, threading.Lock()


def secret_key():
    if config.SECRET_KEY:
        return config.SECRET_KEY
    if config.APP_PASSWORD:      # stable across restarts without storing anything
        return hashlib.sha256(("sentry-session|" + config.APP_PASSWORD + "|" + config.DATABASE_URL).encode()).hexdigest()
    return os.urandom(32).hex()


def login_enabled():
    return bool(config.APP_PASSWORD)


def _client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0]).strip()


def try_login(password):
    """Constant-time password check with a simple per-IP lockout (5 failures / 5 minutes)."""
    ip, now = _client_ip(), time.time()
    with _fail_lock:
        q = _fails.setdefault(ip, deque())
        while q and now - q[0] > 300:
            q.popleft()
        if len(q) >= 5:
            return False, "Too many attempts - wait 5 minutes"
    if password and hmac.compare_digest(password.encode(), config.APP_PASSWORD.encode()):
        session.clear()
        session["auth"] = True
        session.permanent = True
        return True, None
    with _fail_lock:
        _fails[ip].append(now)
    return False, "Wrong password"


OPEN_PATHS = ("/health", "/login", "/api/login", "/api/gateway/")


def guard():
    """before_request hook: login wall + same-origin check for state-changing calls."""
    p = request.path
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and not p.startswith("/api/gateway/"):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/") and \
                origin.replace("http://", "https://").rstrip("/") != request.host_url.replace("http://", "https://").rstrip("/"):
            return jsonify(error="Cross-site request blocked"), 403
    if not login_enabled() or p.startswith(OPEN_PATHS) or p.startswith("/static/") or session.get("auth"):
        return None
    if p.startswith("/api/"):
        return jsonify(error="Login required", login=True), 401
    return None      # index.html loads and shows the login form itself


# ------------------------------------------------------------------ gateway HMAC
def sign(token, method, path, body_bytes, ts=None, nonce=None):
    ts = str(int(ts or time.time()))
    nonce = nonce or os.urandom(12).hex()
    msg = "\n".join([ts, nonce, method.upper(), path, hashlib.sha256(body_bytes or b"").hexdigest()])
    return ts, nonce, hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()


def verify_gateway():
    """Returns None if OK, else a (response, code) tuple."""
    if not config.GATEWAY_TOKEN:
        return jsonify(error="Gateway not configured: set GATEWAY_TOKEN on the server"), 503
    ts, nonce, sig = (request.headers.get(h, "") for h in ("X-Sentry-Ts", "X-Sentry-Nonce", "X-Sentry-Sig"))
    try:
        if abs(time.time() - int(ts)) > SIG_WINDOW:
            return jsonify(error="Request too old or clock skew > 5 min"), 401
    except ValueError:
        return jsonify(error="Missing signature"), 401
    _, _, expect = sign(config.GATEWAY_TOKEN, request.method, request.path, request.get_data(), ts, nonce)
    if not hmac.compare_digest(expect, sig):
        return jsonify(error="Bad signature"), 401
    now = time.time()
    with _nonce_lock:
        for k in [k for k, t in _nonces.items() if now - t > SIG_WINDOW * 2]:
            _nonces.pop(k, None)
        if nonce in _nonces:
            return jsonify(error="Replayed request"), 401
        _nonces[nonce] = now
    return None
