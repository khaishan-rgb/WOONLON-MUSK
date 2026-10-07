# S.E.N.T.R.Y — AI Options Intelligence & Decision System (V3)

A decision-support web app for US stock options. It answers four questions in plain English:

| Question | Possible answers |
|---|---|
| **BUY** – what should I consider buying now? | BUY NOW · BUY IF TRIGGERED (at or below a price) · or **NO TRADE TODAY** |
| **HOLD** – keep my current option? | HOLD · HOLD / WATCH · ADD / BUY |
| **SELL** – take profit or cut? | TAKE 25% / 50% PROFIT · SELL · EXIT NOW |
| **WAIT** – better to do nothing? | WAIT · AVOID |

Every decision shows the contract, entry range, profit and exit levels, maximum loss, reasons for and against,
scenario outcomes, data freshness, and what would change the AI's mind. Probabilities are model estimates — never
guarantees — and the Trade Journal measures whether they are honest over time.

## What's new in V3
- **Moomoo integration** through the official OpenD gateway (read-only by default): real positions, cash, buying
  power, option chains with bid/ask, IV and Greeks. See `MOOMOO_SETUP.md`.
- **Command Dashboard**: WHAT TO BUY | WHAT TO HOLD | WHAT TO SELL | WHAT TO AVOID.
- **Risk engine (Engine F)**: position size, sector concentration, correlation, buying power — it can veto any trade.
- **Options Simulator**: your contract, your assumptions, values at several exit dates, distribution and break-even chart.
- **Backtesting**: walk-forward, out-of-sample, with benchmarks and honest labelling (option prices are modelled).
- **Paper Trading**: fees, ask/bid fills, reset with a permanent audit trail, clearly separate from real holdings.
- **Order tickets** (Phase 3): manual approval with a typed confirmation; Moomoo paper account by default; real money
  needs three separate switches. Nothing is ever placed silently.
- **Alerts**: custom triggers, acknowledgement, de-duplication, optional phone push via webhook.
- **Security**: login password, signed + replay-protected gateway requests, no secrets in the browser or database.
- **12–18-month expiries** preferred by default (new YEAR mode), shorter modes still available.

## Run locally
```bash
pip install -r requirements.txt
DATA_MODE=demo python app.py         # synthetic data, works offline -> http://localhost:8000
python app.py                        # real data (free delayed feeds + Moomoo if the gateway runs)
python test_sentry.py                # automated tests (offline, standard library only)
```
Windows PowerShell: `$env:DATA_MODE="demo"; python app.py`

## Deploy
See `RENDER_DEPLOY.txt`. The repo works flat (all files in the root) — GitHub's web uploader drops folders.

## Files
| File | Purpose |
|---|---|
| `app.py` | Web server, API routes, login, gateway endpoints |
| `moomoo_gateway.py` | **Runs on your computer** next to moomoo OpenD; pushes signed data to the website |
| `moomoo_store.py` | Server side of Moomoo: storage, freshness labels, data adapters, position sync |
| `orders.py` | Manual-approved order tickets |
| `risk.py` | Portfolio risk engine |
| `optsim.py` | Options profit simulator |
| `backtest.py` | Walk-forward backtest (modelled option prices) |
| `analysis.py`, `decisions.py`, `engines.py`, `simulate.py`, `pricing.py` | Analysis engines, entry/hold/exit logic, Monte Carlo, pricing |
| `providers.py` | Data adapters with failover: Moomoo → Cboe → Yahoo; SEC EDGAR; FRED; Finnhub |
| `services.py`, `worker.py`, `db.py` | Business logic, background jobs, SQLite/PostgreSQL |
| `index.html`, `app.js`, `style.css` | The dashboard |
| `test_sentry.py`, `fake_moomoo_sdk.py` | Tests and the offline stand-in for the Moomoo SDK used only by tests |

## Environment variables (server)
| Variable | Purpose |
|---|---|
| `DATA_MODE` | `live` (default) or `demo` |
| `DATABASE_URL` | PostgreSQL URL on Render (SQLite file locally) |
| `APP_PASSWORD` | Login password for the website — set it |
| `SECRET_KEY` | Signs the login cookie |
| `GATEWAY_TOKEN` | Shared secret with `moomoo_gateway.py` |
| `ALLOW_REAL_ORDERS` | `0` (default). `1` lets confirmed real-money tickets through |
| `SEC_USER_AGENT` | "Name email" for SEC EDGAR |
| `FINNHUB_API_KEY` | Optional second price source + news |
| `ALERT_WEBHOOK_URL` | Optional push notifications (e.g. an ntfy.sh topic URL) |

## Honest limits
- **Free data is delayed** (~15 min) and Yahoo often blocks cloud servers; Cboe is the free fallback. With the Moomoo
  gateway running, option data comes from your own Moomoo quote rights.
- **US options quotes on Moomoo** are free (LV1) only if your account has assets or US positions; otherwise Moomoo
  sells an OPRA quote card. The gateway reports whether quotes actually came back.
- **Backtests use modelled option prices** — no free source has historical option quotes. The app says so on the page.
- **Probabilities are uncalibrated** until the Trade Journal has enough closed outcomes; reports say which.
- **Tested offline only** with a stand-in for the Moomoo SDK built from the official documentation; the first run
  against a real OpenD may need a small fix. `python moomoo_gateway.py --selftest` checks it without sending anything.

Research and decision-support tool. Not financial advice. Options can lose 100% of the premium.
