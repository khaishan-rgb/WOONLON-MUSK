"""Settings for the Options Opportunity Machine. Edit freely."""
import os

# ---------------- Universe ----------------
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",
    "AMD", "AVGO", "NFLX", "JPM", "XOM", "UNH", "LLY", "COST", "MU", "CRM",
    "ORCL", "PLTR", "UBER", "TSM", "CAT",
]
BENCHMARK = "SPY"

# ---------------- Free API keys (all optional) ----------------
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")          # free at finnhub.io (news, 2nd price source)
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "OptionsMachine research admin@example.com")  # SEC asks for name+email
HTTP_TIMEOUT = 20
REQUEST_PAUSE = 0.4            # seconds between Yahoo calls (be polite, avoid blocks)

# ---------------- Contract filters ----------------
MIN_DTE, MAX_DTE = 30, 540      # few weeks to ~18 months
MAX_EXPIRIES = 6
MIN_OPEN_INTEREST = 100
MAX_SPREAD_PCT = 0.10           # (ask-bid)/mid
MIN_PREMIUM = 0.10
TARGET_DELTAS = [0.70, 0.55, 0.40, 0.30]   # slightly ITM, ATM, slightly OTM, OTM
MIN_DELTA = 0.20                # below this = lottery ticket -> rejected
ALLOW_HIGH_SPECULATION = False
STALE_QUOTE_DAYS = 3

# ---------------- Simulation ----------------
N_PATHS = 20000                 # per regime (spec: 10k-100k)
SEED = 7
SPOT_VOL_BETA = 0.4             # IV falls when stock rises (and vice versa)
INVESTMENT = 1000.0

# annual drift, vol multiplier, jumps/yr, jump mean, jump sd, IV multiplier, earnings bias
REGIMES = {
    "normal":        dict(mu=0.07,  vol_mult=1.0, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.00, earn_bias=0),
    "bull":          dict(mu=0.25,  vol_mult=0.9, jump_lambda=1.0, jump_mean=0.01,  jump_sd=0.04, iv_mult=0.95, earn_bias=0),
    "bear":          dict(mu=-0.20, vol_mult=1.2, jump_lambda=2.0, jump_mean=-0.04, jump_sd=0.06, iv_mult=1.15, earn_bias=0),
    "high_vol":      dict(mu=0.00,  vol_mult=1.6, jump_lambda=3.0, jump_mean=-0.03, jump_sd=0.08, iv_mult=1.30, earn_bias=0),
    "low_vol":       dict(mu=0.10,  vol_mult=0.7, jump_lambda=0.5, jump_mean=0.00,  jump_sd=0.03, iv_mult=0.85, earn_bias=0),
    "crash":         dict(mu=-0.10, vol_mult=2.0, jump_lambda=6.0, jump_mean=-0.08, jump_sd=0.10, iv_mult=1.80, earn_bias=0),
    "iv_expansion":  dict(mu=0.05,  vol_mult=1.1, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.35, earn_bias=0),
    "iv_crush":      dict(mu=0.05,  vol_mult=0.9, jump_lambda=0.5, jump_mean=0.00,  jump_sd=0.03, iv_mult=0.70, earn_bias=0),
    "rate_shock":    dict(mu=-0.08, vol_mult=1.3, jump_lambda=2.0, jump_mean=-0.03, jump_sd=0.06, iv_mult=1.20, earn_bias=0),
    "earnings_beat": dict(mu=0.07,  vol_mult=1.0, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.00, earn_bias=1),
    "earnings_miss": dict(mu=0.07,  vol_mult=1.0, jump_lambda=1.0, jump_mean=-0.02, jump_sd=0.05, iv_mult=1.00, earn_bias=-1),
}
REGIME_WEIGHTS = {
    "normal": 0.30, "bull": 0.12, "bear": 0.12, "high_vol": 0.08, "low_vol": 0.08, "crash": 0.03,
    "iv_expansion": 0.06, "iv_crush": 0.08, "rate_shock": 0.05, "earnings_beat": 0.04, "earnings_miss": 0.04,
}

# ---------------- Scoring (spec section 20) ----------------
SCORE_WEIGHTS = {
    "fundamentals": 0.15, "growth": 0.10, "valuation": 0.10, "catalysts": 0.10, "macro": 0.10,
    "technical": 0.10, "options_pricing": 0.15, "mc_ev": 0.10, "liquidity": 0.05, "risk_reward": 0.05,
}
BUY_MIN_SCORE = 70
WATCH_MIN_SCORE = 60
RISK_BUDGET_PCT = {"A+": 5.0, "A": 4.0, "B": 2.5, "C": 1.0, "D": 0.0}
