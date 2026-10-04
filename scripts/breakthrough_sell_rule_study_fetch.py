"""Fetch a 2016 cohort for the breakthrough sell-rule study (read-only FMP). Cached to disk."""
import sys, json, os, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,'.'); sys.path.insert(0,'tools')
import screen_universe as su
S=os.path.dirname(os.path.abspath(__file__))
capf, pxf = S+'/bt_caps.json', S+'/bt_px.json'
sc=su._fmp_get("company-screener","",{"marketCapMoreThan":300000000,"country":"US","isEtf":"false","isFund":"false","limit":10000}) or []
syms=sorted({r['symbol'] for r in sc if r.get('exchangeShortName') in ('NASDAQ','NYSE','AMEX') and r.get('isActivelyTrading') and '-' not in r['symbol'] and '.' not in r['symbol']})
meta={r['symbol']:(r.get('sector'),r.get('marketCap')) for r in sc}
caps=json.load(open(capf)) if os.path.exists(capf) else {}
def cap(s):
    a=su._fmp_get("historical-market-capitalization",s,{"from":"2016-01-04","to":"2016-01-12"}) or []
    return s,(a[-1].get('marketCap') if a else None)
todo=[s for s in syms if s not in caps]
print('symbols',len(syms),'caps to fetch',len(todo),flush=True)
with ThreadPoolExecutor(4) as ex:
    for i,(s,c) in enumerate(ex.map(cap,todo)):
        caps[s]=c
        if i%500==499: json.dump(caps,open(capf,'w')); print('caps',i+1,flush=True)
json.dump(caps,open(capf,'w'))
cohort=[s for s in syms if caps.get(s) and 1e9<=caps[s]<=50e9]
print('cohort $1-50B in Jan 2016:',len(cohort),flush=True)
px=json.load(open(pxf)) if os.path.exists(pxf) else {}
def hist(s):
    h=su._fmp_get("historical-price-eod/light",s,{"from":"2015-01-02","to":"2026-10-02"}) or []
    return s,[(r['date'],r['price']) for r in reversed(h) if r.get('price')]
todo=[s for s in cohort+['SPY'] if s not in px]
with ThreadPoolExecutor(4) as ex:
    for i,(s,h) in enumerate(ex.map(hist,todo)):
        px[s]=h
        if i%300==299: json.dump(px,open(pxf,'w')); print('px',i+1,flush=True)
json.dump(px,open(pxf,'w')); json.dump(meta,open(S+'/bt_meta.json','w'))
print('done: price series',len(px),flush=True)
