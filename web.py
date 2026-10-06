import os, time, uuid, threading, traceback
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, render_template_string

import config, engines, report
from data_sources import stamp

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
jobs = {}
lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=int(os.getenv('SCAN_WORKERS','2')))

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Options Opportunity Machine</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#071019;color:#eaf6ff;font-family:Inter,system-ui,Arial}.wrap{max-width:1180px;margin:auto;padding:28px}.hero{padding:28px;border:1px solid #17415d;border-radius:18px;background:linear-gradient(135deg,#0a1825,#071019);box-shadow:0 0 40px #00aaff18}.eyebrow{color:#6fd8ff;letter-spacing:.14em;font-size:12px;font-weight:800}.hero h1{margin:8px 0;font-size:clamp(28px,5vw,52px)}.sub{color:#a9c2d3;max-width:850px}.panel{margin-top:18px;padding:20px;border:1px solid #17384d;border-radius:16px;background:#0a151f}.grid{display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:12px}label{font-size:12px;color:#8fb2c8;display:block;margin-bottom:6px}input,select,button{width:100%;padding:12px;border-radius:10px;border:1px solid #23516b;background:#08131d;color:#fff}button{background:#0aa8e8;border:0;font-weight:800;cursor:pointer;align-self:end}button:disabled{opacity:.5}.status{margin-top:14px;color:#7bdcff}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}.card{padding:16px;border:1px solid #17384d;border-radius:14px;background:#0a151f}.big{font-size:24px;font-weight:900}.buy{color:#5df0a5}.watch{color:#ffd66d}.avoid{color:#ff7d8c}pre{white-space:pre-wrap;word-wrap:break-word;background:#050c12;border-radius:12px;padding:18px;max-height:65vh;overflow:auto;color:#d8eaf5}.note{font-size:12px;color:#7895a7;margin-top:12px}@media(max-width:800px){.grid,.cards{grid-template-columns:1fr}.wrap{padding:14px}}
</style></head><body><div class="wrap"><div class="hero"><div class="eyebrow">PROBABILITY-WEIGHTED OPTIONS RESEARCH</div><h1>AI Options Opportunity Machine</h1><div class="sub">Scan selected U.S. tickers using the existing fundamentals, macro, technical, option-pricing, Monte Carlo, backtest and investment-committee engines. Research tool only; options can lose 100% of premium.</div></div>
<div class="panel"><div class="grid"><div><label>Tickers (space or comma separated)</label><input id="tickers" value="NVDA GOOGL AMZN META MSFT"></div><div><label>Mode</label><select id="mode"><option value="live">Live market data</option><option value="demo">Offline demo</option></select></div><div><label>Monte Carlo paths / regime</label><select id="paths"><option>5000</option><option selected>10000</option><option>20000</option><option>50000</option></select></div><div><label>&nbsp;</label><button id="scan" onclick="startScan()">SCAN MARKET</button></div></div><div class="note">Start with 3–5 tickers on Render. Larger universes and 50k paths can take considerably longer.</div><div class="status" id="status">Ready.</div></div>
<div class="cards" id="cards" style="display:none"><div class="card"><div>Top action</div><div class="big" id="action">—</div></div><div class="card"><div>Top ticker</div><div class="big" id="ticker">—</div></div><div class="card"><div>Score</div><div class="big" id="score">—</div></div><div class="card"><div>Confidence</div><div class="big" id="confidence">—</div></div></div>
<div class="panel" id="resultPanel" style="display:none"><h2>Layman Report</h2><pre id="report"></pre></div></div>
<script>
async function startScan(){let b=document.getElementById('scan');b.disabled=true;document.getElementById('resultPanel').style.display='none';document.getElementById('cards').style.display='none';let tickers=document.getElementById('tickers').value.split(/[ ,]+/).filter(Boolean);let demo=document.getElementById('mode').value==='demo';let paths=+document.getElementById('paths').value;document.getElementById('status').textContent='Starting scan…';try{let r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tickers,demo,paths})});let j=await r.json();if(!r.ok)throw Error(j.error||'Unable to start');poll(j.job_id)}catch(e){document.getElementById('status').textContent='Error: '+e.message;b.disabled=false}}
async function poll(id){try{let r=await fetch('/api/jobs/'+id),j=await r.json();document.getElementById('status').textContent=j.status==='running'?'Scanning: '+(j.progress||'working…'):j.status;if(j.status==='done'){document.getElementById('report').textContent=j.report;document.getElementById('resultPanel').style.display='block';if(j.top){let a=document.getElementById('action');a.textContent=j.top.action;a.className='big '+(j.top.action==='BUY'?'buy':j.top.action==='WATCH'?'watch':'avoid');document.getElementById('ticker').textContent=j.top.ticker;document.getElementById('score').textContent=Math.round(j.top.score||0)+'/100';document.getElementById('confidence').textContent=Math.round(j.top.confidence||0)+'/100';document.getElementById('cards').style.display='grid'}document.getElementById('scan').disabled=false;return}if(j.status==='error'){document.getElementById('status').textContent='Error: '+j.error;document.getElementById('scan').disabled=false;return}setTimeout(()=>poll(id),1500)}catch(e){document.getElementById('status').textContent='Connection error: '+e.message;document.getElementById('scan').disabled=false}}
</script></body></html>'''

def setjob(jid, **kw):
    with lock:
        jobs.setdefault(jid, {}).update(kw)

def run_scan(jid, tickers, demo, paths):
    try:
        run_time = stamp()
        if demo:
            import demo as dm
            fred, bench = dm.make_fred(), dm.make_bench()
            macro = engines.macro_engine(fred)
            universe = dm.make_universe(macro['r'])
            if tickers:
                wanted={x.upper() for x in tickers}; universe=[x for x in universe if x.ticker.upper() in wanted] or universe
            loader=lambda td: td
        else:
            import data_sources as ds
            setjob(jid, progress='Loading macro data…')
            macro=engines.macro_engine(ds.fetch_fred())
            try: bench=ds.fetch_history_only(config.BENCHMARK)
            except Exception: bench=None
            universe=tickers or config.DEFAULT_UNIVERSE[:5]
            loader=ds.load_ticker
        results=[]
        for i,item in enumerate(universe,1):
            name=item if isinstance(item,str) else item.ticker
            setjob(jid, progress=f'{i}/{len(universe)} — {name}')
            try:
                td=loader(item); results += engines.analyse(td,macro,bench,paths)
            except Exception as ex:
                results.append(dict(ticker=name, action='AVOID', error=f'{ex.__class__.__name__}: {ex}'))
            if not demo: time.sleep(config.REQUEST_PAUSE)
        txt=report.build(results,demo=demo,run_time=run_time)
        traded=sorted([r for r in results if 'trade' in r],key=lambda r:r.get('score',0),reverse=True)
        buys=[r for r in traded if r.get('action')=='BUY']
        top=(buys or traded or [None])[0]
        topj=None if top is None else {k:top.get(k) for k in ('ticker','action','score','grade','confidence','dq')}
        setjob(jid,status='done',progress='Complete',report=txt,top=topj,finished=time.time())
    except Exception as ex:
        setjob(jid,status='error',error=f'{ex.__class__.__name__}: {ex}',trace=traceback.format_exc(),finished=time.time())

@app.get('/')
def index(): return render_template_string(HTML)

@app.get('/health')
def health(): return jsonify(status='ok', service='options-machine')

@app.post('/api/scan')
def scan():
    d=request.get_json(silent=True) or {}
    tickers=[str(x).strip().upper() for x in d.get('tickers',[]) if str(x).strip()][:10]
    demo=bool(d.get('demo',False))
    try: paths=int(d.get('paths',10000))
    except Exception: paths=10000
    paths=max(1000,min(paths,50000))
    jid=uuid.uuid4().hex[:12]
    setjob(jid,status='running',progress='Queued',created=time.time())
    executor.submit(run_scan,jid,tickers,demo,paths)
    return jsonify(job_id=jid,status='running'),202

@app.get('/api/jobs/<jid>')
def job(jid):
    with lock: j=jobs.get(jid)
    if not j: return jsonify(error='Job not found'),404
    return jsonify(j)

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
