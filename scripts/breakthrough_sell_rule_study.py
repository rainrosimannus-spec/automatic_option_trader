"""Sell-rule study on a breakthrough-like cohort: do nothing vs stops vs profit-taking vs rebalancing."""
import json, os, sys, statistics as st, bisect
S=os.path.dirname(os.path.abspath(__file__))
px=json.load(open(S+'/bt_px.json')); caps=json.load(open(S+'/bt_caps.json'))
spy=px.pop('SPY'); sd=[d for d,_ in spy]; sp=[p for _,p in spy]
def spy_at(date):
    i=bisect.bisect_left(sd,date); return sp[min(i,len(sp)-1)]
END=sp[-1]
def series(sym,start):
    h=px.get(sym) or []
    d=[x[0] for x in h]; i=bisect.bisect_left(d,start)
    if i>=len(h) or d[i]>start[:8]+'20' or len(h)-i<200: return None
    return h[i:]
def vol_before(sym,start):
    h=px.get(sym) or []; d=[x[0] for x in h]; i=bisect.bisect_left(d,start)
    w=h[max(0,i-252):i]
    if len(w)<150: return None
    r=[w[k+1][1]/w[k][1]-1 for k in range(len(w)-1) if w[k][1]>0]
    return st.pstdev(r)*(252**0.5)
def to_spy(value,date): return value*END/spy_at(date)
def hold(h): return h[-1][1]/h[0][1]
def trailing(h,x):
    p0=h[0][1]; peak=p0
    for d,p in h:
        if p>peak: peak=p
        elif p<=peak*(1-x): return to_spy(p/p0,d)
    return h[-1][1]/p0
def stop_cost(h,x):
    p0=h[0][1]
    for d,p in h:
        if p<=p0*(1-x): return to_spy(p/p0,d)
    return h[-1][1]/p0
def take_profit(h,k,frac):
    p0=h[0][1]
    for d,p in h:
        if p>=k*p0: return frac*to_spy(p/p0,d)+(1-frac)*h[-1][1]/p0
    return h[-1][1]/p0
def rebalance(H):
    # annual rebalance to equal weight across the cohort (sell winners, buy losers)
    n=len(H); val=[1.0]*n; L=min(len(h) for h in H); step=252; t=0
    while t+step<L:
        tot=sum(val[i]*H[i][t+step][1]/H[i][t][1] for i in range(n)); val=[tot/n]*n; t+=step
    return sum(val[i]*H[i][-1][1]/H[i][t][1] for i in range(n))/n   # (uses aligned index; approx.)
def study(start,label,subset):
    rows=[]
    for s in subset:
        h=series(s,start)
        if h and h[0][1]>1: rows.append((s,h))
    if len(rows)<30: print(label,'too few',len(rows)); return
    n=len(rows); H=[h for _,h in rows]
    mult=sorted(((hold(h),s) for s,h in rows),reverse=True)
    res={'do nothing':sum(m for m,_ in mult)/n,
         'trailing stop 30%':sum(trailing(h,.30) for h in H)/n,
         'trailing stop 50%':sum(trailing(h,.50) for h in H)/n,
         'stop at -50% from cost':sum(stop_cost(h,.50) for h in H)/n,
         'sell half at 3x':sum(take_profit(h,3,.5) for h in H)/n,
         'sell all at 5x':sum(take_profit(h,5,1.0) for h in H)/n,
         'rebalance yearly':rebalance(H),
         'market fund instead':END/spy_at(start)}
    ten=[s for m,s in mult if m>=10]; five=sum(1 for m,_ in mult if m>=5); half=sum(1 for m,_ in mult if m<=0.5)
    top=max(1,n//20); profit=sum(m-1 for m,_ in mult); topshare=sum(m-1 for m,_ in mult[:top])/profit if profit>0 else float('nan')
    print(f"\n== {label}: {n} names, start {start}")
    for k,v in res.items(): print(f"   {k:24s} {v:6.2f}x" + ("" if k=='do nothing' else f"   ({(v/res['do nothing']-1)*100:+.0f}% vs do nothing)"))
    print(f"   tenfold+: {len(ten)} ({len(ten)/n*100:.0f}%) | fivefold+: {five/n*100:.0f}% | lost half or more: {half/n*100:.0f}% | median {mult[n//2][0]:.2f}x | top 5% of names made {topshare*100:.0f}% of all profit")
    # the winners' road
    def dd_before_10x(h):
        p0=h[0][1]; peak=p0; mdd=0
        for d,p in h:
            peak=max(peak,p); mdd=max(mdd,1-p/peak)
            if p>=10*p0: break
        return mdd
    if ten:
        d=[dd_before_10x(h) for s,h in rows if s in ten]
        print(f"   the {len(ten)} tenfold winners: median worst drawdown on the way {st.median(d)*100:.0f}% | fell 30%+ on the way: {sum(1 for x in d if x>=.3)}/{len(d)} | fell 50%+: {sum(1 for x in d if x>=.5)}/{len(d)}")
        print('   winners:', ' '.join(f"{s}:{m:.0f}x" for m,s in mult[:12]))
    return res
cohort=[s for s in px if caps.get(s) and 1e9<=caps[s]<=50e9]
for start in ('2016-01-04','2019-01-02','2021-02-12'):
    v={s:vol_before(s,start) for s in cohort}; vv=sorted(x for x in v.values() if x)
    if not vv: continue
    cut=vv[int(len(vv)*2/3)]
    study(start,'ALL $1-50B (2016 size)',cohort)
    study(start,'MOST VOLATILE THIRD (breakthrough-like)',[s for s in cohort if v.get(s) and v[s]>=cut])
