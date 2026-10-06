"""Monte Carlo future simulator (spec 9, 10, 17).

Fat tails (Student-t), jump diffusion, per-path random volatility, scheduled earnings jumps,
11 market regimes, IV that moves with regime and spot, bid/ask costs on entry and exit.
"""
import numpy as np
import config
from pricing import bs_price


def cal_to_td(cal_days):
    return max(1, int(round(cal_days * 252 / 365)))


def simulate_regimes(S0, sigma, checkpoints_td, n, earn_td=None, earn_sd=0.05, seed=config.SEED):
    """Simulate n paths per regime; only store prices on checkpoint days (memory-friendly)."""
    rng = np.random.default_rng(seed)
    cps = sorted({int(c) for c in checkpoints_td if c >= 1})
    want, maxd = set(cps), cps[-1]
    dt, sq = 1 / 252, np.sqrt(1 / 252)
    out = {}
    for name, g in config.REGIMES.items():
        sig = sigma * g["vol_mult"] * np.exp(0.25 * rng.standard_normal(n) - 0.03125)
        drift = (g["mu"] - 0.5 * sig ** 2) * dt
        logS = np.full(n, np.log(S0))
        store = {}
        for d in range(1, maxd + 1):
            step = drift + sig * sq * rng.standard_t(4, n) / np.sqrt(2.0)
            nj = rng.poisson(g["jump_lambda"] * dt, n)
            hit = nj > 0
            if hit.any():
                k = nj[hit]
                step[hit] += g["jump_mean"] * k + g["jump_sd"] * np.sqrt(k) * rng.standard_normal(k.size)
            if earn_td is not None and d == earn_td and earn_sd > 0:
                b = g["earn_bias"]
                step += rng.normal(b * 0.8 * earn_sd, 0.6 * earn_sd, n) if b else rng.normal(0, earn_sd, n)
            logS += step
            if d in want:
                store[d] = np.exp(logS)
        out[name] = store
    return out


def _earn_iv_drop(dte):
    """After earnings, front-month IV deflates; long-dated much less."""
    return min(0.25, 0.10 * 60 / max(dte, 1))


def position_value(trade, S_t, t_cal, ctx, regime="normal", exit_side=True, iv_override_mult=None):
    """Per-share value of the position at t_cal calendar days from now."""
    g = config.REGIMES[regime]
    S_t = np.asarray(S_t, dtype=float)
    total = np.zeros_like(S_t)
    for leg in trade["legs"]:
        T = max(leg["dte"] - t_cal, 0) / 365
        frac = min(1.0, t_cal / 90)
        mult = iv_override_mult if iv_override_mult is not None else 1 + (g["iv_mult"] - 1) * frac
        iv = leg["iv"] * mult * (S_t / ctx["S0"]) ** (-config.SPOT_VOL_BETA)
        ec = ctx.get("earn_cal")
        if ec is not None and ec <= t_cal < leg["dte"] and ec < leg["dte"]:
            iv = iv * (1 - _earn_iv_drop(leg["dte"]))
        iv = np.clip(iv, 0.05, 3.0)
        v = bs_price(S_t, leg["K"], T, ctx["r"], iv, leg["kind"], ctx["q"])
        if exit_side:  # longs sell at bid, shorts buy back at ask
            v = v * (1 - leg["sign"] * leg["hs"])
        total = total + leg["sign"] * v
    return np.maximum(total, 0.0)


def _pooled(trade, sims, ctx, td_idx, t_cal, exit_side=True, exits=False):
    """Pool regimes by weight. With exits=True, apply the exit plan (profit target / -50% stop)
    at each monitoring date before td_idx, like a disciplined trader would."""
    R, P = [], []
    for name, w in ctx["weights"].items():
        S_all = sims[name][td_idx]
        m = int(round(w * len(S_all)))
        if m <= 0:
            continue
        S = S_all[:m]
        r_end = position_value(trade, S, t_cal, ctx, name, exit_side) / trade["cost"] - 1
        if exits:
            out = np.full(m, np.nan)
            for d in trade["monitor_td"]:
                if d >= td_idx or d not in sims[name]:
                    continue
                r = position_value(trade, sims[name][d][:m], d * 365 / 252, ctx, name, True) / trade["cost"] - 1
                hit = np.isnan(out) & ((r >= trade["target_ret"]) | (r <= -0.5))
                out[hit] = r[hit]
            r_end = np.where(np.isnan(out), r_end, out)
        R.append(r_end)
        P.append(S)
    return np.concatenate(R), np.concatenate(P)


def evaluate(trade, sims, ctx):
    """Probabilities and EV when following the exit plan, plus no-rules and hold-to-expiry views."""
    R, P = _pooled(trade, sims, ctx, trade["horizon_td"], trade["horizon_cal"], exits=True)
    Rn, _ = _pooled(trade, sims, ctx, trade["horizon_td"], trade["horizon_cal"])
    Re, _ = _pooled(trade, sims, ctx, trade["expiry_td"], trade["dte"], exit_side=False)
    down = np.sqrt(np.mean(np.minimum(R, 0) ** 2)) or 1e-9
    q = lambda a, p: float(np.percentile(a, p))
    return dict(
        p_profit=float(np.mean(R > 0)), p_25=float(np.mean(R >= 0.25)), p_50=float(np.mean(R >= 0.5)),
        p_100=float(np.mean(R >= 1.0)), p_200=float(np.mean(R >= 2.0)),
        p_loss50=float(np.mean(R <= -0.5)), p_total_loss=float(np.mean(R <= -0.95)),
        p_total_loss_no_stop=float(np.mean(Rn <= -0.95)), p_profit_no_rules=float(np.mean(Rn > 0)),
        mean=float(np.mean(R)), median=q(R, 50), q10=q(R, 10), q75=q(R, 75), q95=q(R, 95),
        sortino=float(np.mean(R) / down), ev_dollars=float(np.mean(R) * config.INVESTMENT),
        stock_q10=q(P, 10), stock_q50=q(P, 50), stock_q90=q(P, 90),
        p_profit_expiry=float(np.mean(Re > 0)), n_paths=int(len(R)),
    )


def time_path(trade, sims, ctx):
    """Spec 10: how stock, theta, IV and delta move the option over time."""
    rows = []
    days = [d for d in (1, 5, 21, 63, 126, 252) if d < trade["expiry_td"]] + [trade["expiry_td"]]
    labels = {1: "1 day", 5: "1 week", 21: "1 month", 63: "3 months", 126: "6 months", 252: "12 months"}
    for d in days:
        if d not in sims["normal"]:
            continue
        t_cal = trade["dte"] if d == trade["expiry_td"] else d * 365 / 252
        R, P = _pooled(trade, sims, ctx, d, t_cal, exit_side=False)
        smed = float(np.median(P))
        vmed = float(np.median((R + 1) * trade["cost"]))
        flat = float(position_value(trade, np.array([ctx["S0"]]), t_cal, ctx, "normal", False)[0])
        up = position_value(trade, np.array([smed * 1.01]), t_cal, ctx, "normal", False)[0]
        dn = position_value(trade, np.array([smed * 0.99]), t_cal, ctx, "normal", False)[0]
        delta = float((up - dn) / (0.02 * smed))
        rows.append(dict(when="Expiry" if d == trade["expiry_td"] else labels[d], stock_median=smed,
                         option_median=vmed, flat_stock_value=flat, delta=delta))
    return rows


def profit_table(trade, ctx, moves=(-0.30, -0.10, 0.0, 0.10, 0.20, 0.40, 0.70)):
    """Spec 17: $1,000 scenarios at the planned time-exit, with IV + time decay (not intrinsic only)."""
    rows = []
    for m in moves:
        S = ctx["S0"] * (1 + m)
        v = float(position_value(trade, np.array([S]), trade["horizon_cal"], ctx, "normal", True)[0])
        val = config.INVESTMENT * v / trade["cost"]
        rows.append(dict(move=m, stock=S, option=v, value=val, ret=val / config.INVESTMENT - 1))
    return rows
