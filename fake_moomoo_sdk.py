"""Fake moomoo SDK for offline tests. Mirrors the documented signatures/columns only."""
import datetime as dt, pandas as pd
RET_OK, RET_ERROR = 0, -1
class _E:
    def __init__(self, **kw): self.__dict__.update(kw)
SecurityFirm = _E(FUTUSG="FUTUSG", FUTUINC="FUTUINC", FUTUSECURITIES="FUTUSECURITIES", NONE="N/A")
TrdMarket = _E(US="US", HK="HK"); TrdEnv = _E(REAL="REAL", SIMULATE="SIMULATE")
TrdSide = _E(BUY="BUY", SELL="SELL"); OrderType = _E(NORMAL="NORMAL"); KLType = _E(K_DAY="K_DAY")
Currency = _E(USD="USD", HKD="HKD")
CALLS = []
def set_all_thread_daemon(x): pass
SPOT = {"US.NVDA": 180.0, "US.MSFT": 410.0, "US.AAPL": 230.0}
def _exp(): 
    d = dt.date.today() + dt.timedelta(days=400); return str(d)
class OpenQuoteContext:
    def __init__(self, host='127.0.0.1', port=11111, is_encrypt=None, security_firm=None): pass
    def close(self): pass
    def get_market_snapshot(self, code_list):
        CALLS.append(("snapshot", len(code_list))); rows = []
        for c in code_list:
            if c in SPOT:
                rows.append(dict(code=c, last_price=SPOT[c], prev_close_price=SPOT[c]*0.99, bid_price=SPOT[c]-0.02, ask_price=SPOT[c]+0.02, volume=1e6, update_time="2026-10-06 15:59:59"))
            else:
                import re
                m = re.match(r"^US\.([A-Z]+)(\d{6})([CP])(\d+)$", c)
                k = int(m.group(4)) / 1000; S = SPOT["US." + m.group(1)]
                from pricing import bs_price
                kind = "call" if m.group(3) == "C" else "put"
                mid = float(bs_price(S, k, 400/365, 0.04, 0.4, kind))
                rows.append(dict(code=c, last_price=mid, prev_close_price=mid*0.98, bid_price=round(mid*0.98,2), ask_price=round(mid*1.02,2), volume=500,
                                 option_open_interest=3000, option_implied_volatility=40.0, option_delta=0.5, option_gamma=0.01, option_theta=-0.05,
                                 option_vega=0.5, option_rho=0.3, update_time="2026-10-06 15:59:59"))
        return RET_OK, pd.DataFrame(rows)
    def get_option_expiration_date(self, code):
        CALLS.append(("expiry", code)); return RET_OK, pd.DataFrame([dict(strike_time=_exp(), option_expiry_date_distance=400)])
    def get_option_chain(self, code, index_option_type=None, start=None, end=None, option_type=None, option_cond_type=None, data_filter=None):
        CALLS.append(("chain", code, start, end)); assert start == end
        S = SPOT[code]; d = dt.date.fromisoformat(start); rows = []
        for k in range(int(S*0.5/5)*5, int(S*1.6), 5):
            for cp, ot in (("C","CALL"),("P","PUT")):
                rows.append(dict(code=f"{code}{d:%y%m%d}{cp}{k*1000}", name="x", lot_size=100, stock_type="DRVT", option_type=ot,
                                 stock_owner=code, strike_time=start, strike_price=float(k), suspension=False))
        return RET_OK, pd.DataFrame(rows)
    def request_history_kline(self, code, start=None, end=None, ktype=None, max_count=1000):
        import numpy as np
        idx = pd.bdate_range(end=dt.date.today(), periods=600); c = SPOT[code]*np.exp(np.cumsum(np.random.default_rng(1).normal(0,0.02,600))); c=c/c[-1]*SPOT[code]
        return RET_OK, pd.DataFrame(dict(time_key=[str(x) for x in idx], open=c, high=c*1.01, low=c*0.99, close=c, volume=1e6)), None
class OpenSecTradeContext:
    def __init__(self, filter_trdmarket=None, host='127.0.0.1', port=11111, is_encrypt=None, security_firm=None):
        CALLS.append(("trade_ctx", security_firm))
    def close(self): pass
    def accinfo_query(self, trd_env=None, acc_id=0, acc_index=0, refresh_cache=False, currency=None, asset_category=None):
        return RET_OK, pd.DataFrame([dict(power=5000.0, total_assets=25000.0, cash=4000.0, market_val=21000.0, currency="USD", unrealized_pl=300.0, realized_pl=50.0)])
    def position_list_query(self, code='', position_market=None, pl_ratio_min=None, pl_ratio_max=None, trd_env=None, acc_id=0, acc_index=0, refresh_cache=False):
        d = dt.date.fromisoformat(_exp())
        return RET_OK, pd.DataFrame([dict(code=f"US.NVDA{d:%y%m%d}C190000", stock_name="NVDA call", qty=2.0, can_sell_qty=2.0, cost_price=21.0, cost_price_valid=True,
                                          average_cost=20.5, diluted_cost=21.0, market_val=4400.0, nominal_price=22.0, pl_ratio=7.3, pl_val=300.0, currency="USD"),
                                     dict(code="US.MSFT", stock_name="Microsoft", qty=10.0, cost_price=380.0, average_cost=380.0, market_val=4100.0, pl_val=300.0, currency="USD")])
    def history_deal_list_query(self, code='', start=None, end=None, trd_env=None, acc_id=0, acc_index=0):
        return RET_OK, pd.DataFrame([dict(code="US.MSFT", qty=10, price=380.0, trd_side="BUY", create_time="2026-09-01 10:00:00")])
    def unlock_trade(self, pwd_unlock): return RET_OK, None
    def place_order(self, price, qty, code, trd_side, order_type=None, adjust_limit=0, trd_env=None, acc_id=0, acc_index=0, remark=None, **kw):
        CALLS.append(("place_order", code, qty, price, trd_env, trd_side))
        return RET_OK, pd.DataFrame([dict(order_id="777", order_status="SUBMITTED", code=code, qty=qty, price=price)])
    def order_list_query(self, order_id="", trd_env=None, acc_id=0, **kw):
        return RET_OK, pd.DataFrame([dict(order_id=order_id, order_status="FILLED_ALL", dealt_qty=1, dealt_avg_price=5.0, last_err_msg="")])
