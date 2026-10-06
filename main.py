"""AI Global Options Opportunity Machine - free-API edition.

Usage:
  python main.py                       # scan the default universe (config.py)
  python main.py --tickers NVDA AAPL   # scan specific tickers
  python main.py --demo                # offline test with SYNTHETIC data
  python main.py --paths 50000         # more Monte Carlo paths (slower)
"""
import argparse
import json
import os
import sys
import time

import config
import engines
import report
from data_sources import stamp


def main():
    ap = argparse.ArgumentParser(description="Options opportunity scanner (research tool, not advice)")
    ap.add_argument("--tickers", nargs="*", help="tickers to scan (default: config.DEFAULT_UNIVERSE)")
    ap.add_argument("--demo", action="store_true", help="run offline on synthetic data")
    ap.add_argument("--paths", type=int, default=config.N_PATHS, help="Monte Carlo paths per regime")
    ap.add_argument("--out", default="reports", help="folder for the saved report")
    args = ap.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    run_time = stamp()
    if args.demo:
        import demo
        fred, bench = demo.make_fred(), demo.make_bench()
        macro = engines.macro_engine(fred)
        universe = demo.make_universe(macro["r"])
        loader = lambda td: td
    else:
        import data_sources as ds
        print("Loading macro data (FRED)...")
        macro = engines.macro_engine(ds.fetch_fred())
        try:
            bench = ds.fetch_history_only(config.BENCHMARK)
        except Exception:
            bench = None
        universe = args.tickers or config.DEFAULT_UNIVERSE
        loader = ds.load_ticker

    results = []
    for i, item in enumerate(universe, 1):
        name = item if isinstance(item, str) else item.ticker
        print(f"[{i}/{len(universe)}] {name} ...", flush=True)
        try:
            td = loader(item)
            results += engines.analyse(td, macro, bench, args.paths)
        except Exception as ex:
            results.append(dict(ticker=name, action="AVOID", error=f"{ex.__class__.__name__}: {ex}"))
        if not args.demo:
            time.sleep(config.REQUEST_PAUSE)

    text = report.build(results, demo=args.demo, run_time=run_time)
    print("\n" + text)
    os.makedirs(args.out, exist_ok=True)
    fname = os.path.join(args.out, f"report_{time.strftime('%Y%m%d_%H%M')}{'_DEMO' if args.demo else ''}")
    with open(fname + ".md", "w", encoding="utf-8") as f:
        f.write(text)
    summary = [{k: r.get(k) for k in ("ticker", "direction", "action", "score", "grade", "confidence", "dq", "error")}
               | ({"contract": report.contract_name(r["trade"]), "cost": r["trade"]["cost"] * 100,
                   "p_profit": r["stats"]["p_profit"], "ev_per_1000": r["stats"]["ev_dollars"],
                   "p_total_loss": r["stats"]["p_total_loss"]} if "trade" in r else {})
               for r in results]
    with open(fname + ".json", "w", encoding="utf-8") as f:
        json.dump(dict(run_time=run_time, demo=args.demo, macro={k: macro[k] for k in ("score", "label", "notes", "r_source")},
                       results=summary), f, indent=2, default=str)
    print(f"\nSaved: {fname}.md and {fname}.json")


if __name__ == "__main__":
    main()
