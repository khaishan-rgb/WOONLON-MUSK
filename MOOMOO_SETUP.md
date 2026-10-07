# Connecting Moomoo (Singapore) to SENTRY

## Why there are two pieces
Moomoo's official API does not offer a REST endpoint you can call from a website. It works through **OpenD**, a
gateway app you install and log in to with your moomoo account (it may ask for an SMS code). Your Python code then
talks to OpenD on `127.0.0.1:11111` (TCP).

A Render web service can't run OpenD reliably: it has no screen to log in, restarts often and changes machines. So:

```
 your computer (or a small always-on VPS)                      Render
 ┌──────────────────────────────┐   HTTPS, signed (HMAC)    ┌──────────────────┐
 │ moomoo OpenD  ◄── TCP ──►  moomoo_gateway.py │ ───────────────────────► │ SENTRY website     │
 └──────────────────────────────┘   push only, no inbound   └──────────────────┘
```
- The gateway **pushes** data out; Render never connects into your network.
- Your moomoo login and trade password stay on your computer. Render only knows `GATEWAY_TOKEN`.
- Requests are signed and expire after 5 minutes; replays are rejected.

## Step by step (about 15 minutes)
1. **OpenD**: download from <https://www.moomoo.com/download/OpenAPI>, install, log in, complete the API
   questionnaire on first login. Keep it running.
2. **Python on your computer**: `pip install moomoo-api requests pandas`
3. **Copy two files** from this project to a folder on your computer: `moomoo_gateway.py` and `gateway.env.example`.
   Rename `gateway.env.example` to `gateway.env` and fill in:
   - `SENTRY_URL` = your Render address
   - `GATEWAY_TOKEN` = the value from Render → Environment (Blueprint generates one)
   - `MOOMOO_SECURITY_FIRM=FUTUSG` for moomoo Singapore
4. **Self-test** (reads from OpenD, sends nothing): `python moomoo_gateway.py --selftest`
   It reports whether stock quotes, option chains, option bid/ask and your account came back.
5. **Run it**: `python moomoo_gateway.py` and leave the window open (or set it up as a startup task).
6. In SENTRY open **Settings & API** — "Gateway online" should turn ✅ within a minute, and **Moomoo Portfolio**
   shows your positions with an AI decision for each option.

## What is fetched (official API calls only)
| Data | Moomoo call | Documented limit respected |
|---|---|---|
| Stock & option quotes, IV, Greeks, OI | `get_market_snapshot` | 400 codes / call, 60 calls / 30 s |
| Expiry dates | `get_option_expiration_date` | spaced 3.2 s |
| Option chain contracts | `get_option_chain` (one expiry at a time) | 10 calls / 30 s, ≤ 30-day span |
| Daily candles (fallback when Yahoo is blocked) | `request_history_kline` | once per ticker per day (7-day quota) |
| Funds, buying power | `accinfo_query` (USD) | read-only |
| Positions | `position_list_query` | read-only |
| Recent fills | `history_deal_list_query` | read-only, if available |
| Orders you confirm | `place_order`, `order_list_query` | 15 / 30 s; real orders need `unlock_trade` locally |

## Things that depend on your account (not on SENTRY)
- **US option quotes**: free LV1 if your moomoo account has assets or US positions; otherwise an OPRA quote card is
  required. Without it, option prices fall back to Cboe's free delayed data — the app labels which source is used.
- **Quota**: accounts under HKD 10k get 100 historical-candle tickers per 7 days; the gateway stays within that.
- **Trading**: US options trading must be enabled on your moomoo SG account.

## Orders (optional, off by default)
- **Moomoo paper trading** (`TrdEnv.SIMULATE`): works once the gateway is connected; you still confirm every ticket.
- **Real money** needs all of: `ALLOW_REAL_ORDERS=1` on Render, the Settings switch, and
  `MOOMOO_ALLOW_REAL_ORDERS=1` + `MOOMOO_TRADE_PWD` in `gateway.env`. Each ticket expires 5 minutes after you confirm
  it; the status shown is only what Moomoo returns. SENTRY never sells contracts you don't hold (no naked shorts)
  and never requests withdrawal or transfer permissions.
