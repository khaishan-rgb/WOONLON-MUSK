# AI Global Options Opportunity Machine (free-API edition)

A research scanner that follows your spec: it looks for defined-risk option trades
(long calls/puts, debit spreads) and prints a simple 🟢 BUY / 🟡 HOLD-WATCH / 🔴 SELL-AVOID screen.
Not financial advice. Bought options can lose 100% of what you pay.

## Setup (5 minutes)
    pip install -r requirements.txt
    python main.py --demo                  # offline test, SYNTHETIC data
    python main.py --tickers NVDA AAPL MSFT
    python main.py                         # default universe in config.py

Run it during US market hours (9:30-16:00 ET). Outside them Yahoo often returns
empty bid/ask, and the scanner will refuse to use those quotes.

Optional free keys (set as environment variables):
- `FINNHUB_API_KEY` (finnhub.io): second price source for cross-checking + news sentiment
- `SEC_USER_AGENT="Your Name you@email.com"`: the SEC asks for this on EDGAR requests

Reports are saved to `reports/` as Markdown and JSON.

## Data sources (all free)
| Need | Source | Notes |
|---|---|---|
| Prices, history, option chains | Yahoo via yfinance | Unofficial, ~15 min delayed |
| Fundamentals, shares outstanding | SEC EDGAR XBRL | Official, annual figures |
| Rates, curve, credit, VIX, USD, oil | FRED CSV | No key needed |
| Second price + news | Finnhub | Optional free key |
| Greeks, fair value, simulations | Computed locally | numpy/scipy |

## What each spec section maps to
| Spec | File / function |
|---|---|
| 1 Data + flags | `data_sources.py` (timestamps, stale/conflict flags) |
| 2 Buffett | `engines.fundamental_engine` |
| 3 Soros | `engines.macro_engine` (also tilts simulation regime weights) |
| 4 Catalysts | `engines.catalyst_engine` |
| 5 Expectations | `engines.expectation_engine` (IV vs realised vol, implied vs historical earnings move) |
| 6 Technicals | `engines.technical_engine` |
| 7, 15, 16 Options edge / optimiser | `engines.build_candidates` (ITM/ATM/OTM, spreads, liquidity, lottery filter) |
| 8 Fair value | `pricing.py` (Black-Scholes + binomial American) |
| 9, 10 Monte Carlo + time path | `simulate.py` (11 regimes, fat tails, jumps, random vol, IV moves, earnings jumps) |
| 11 Backtest | `engines.backtest_engine` (stock-level; see limits) |
| 12, 13 Committee + contrarian | `engines.committee` |
| 17 $1,000 simulator | `simulate.profit_table` |
| 18 Exits | `engines.exit_plan` (also enforced inside the simulation) |
| 19, 20 Score + allocation | `engines.analyse` |
| 21, 22 Output | `report.py` |

## Honest limits of the free stack
- Quotes are delayed. Every report says so; always re-check the live price before trading.
- No historical option prices, so the backtest tests the stock signal, not the option.
- "IV percentile" is a proxy (today's IV vs the past year of realised volatility).
- No insider/institutional flow, short-borrow, or unusual-flow data. News sentiment is keyword-based.
- The committee agents are transparent rules, not language models. Every vote is printed.
- Model fair values and probabilities are estimates from assumptions in `config.py`.
  Change the regimes and weights there and the answers change. That's the point: read them as
  "under these assumptions", never as guarantees.
- Default scan: ~25 tickers, about 5-10 minutes. `--paths 5000` is faster, `--paths 50000` is steadier.
