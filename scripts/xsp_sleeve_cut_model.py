import sys, math, statistics as st
S=sys.argv[1] if len(sys.argv)>1 else "data"; src=open("scripts/xsp_stop_rule_conditional.py").read().split("\nWINDOWS = [")[0]; sys.argv=["x",S]; g={}; exec(src,g)
days,ohlc,vix,put=g["days"],g["ohlc"],g["vix"],g["put"]
closes={d:ohlc[d][3] for d in days}
NLV,COMM,VIXMAX,FLOOR,WIDTH=200_000.0,0.87,35.0,0.015,0.03
def drop(i,n,th): return closes[days[i-1]]/closes[days[i-1-n]]-1 < -th
V={"base":(lambda i:0.05)}
for n in (5,):
    for th in (0.02,0.03):
        for f in (0.5,0.25,0.0):
            V[f"{n}d drop>{int(th*100)}% -> x{f}"]=(lambda i,n=n,th=th,f=f: 0.05*f if drop(i,n,th) else 0.05)
def run(d0,d1):
    idx=[i for i,d in enumerate(days) if d0<=d<=d1 and i>=201 and i+1<len(days)]
    out={}
    for name,mf in V.items():
        pnl,eq,peak,dd,n,cut=0.0,0.0,0.0,0.0,0,0
        for i in idx:
            d,dn=days[i],days[i+1]; o=ohlc[d][0]; c1=ohlc[dn][3]; v_prev=vix[days[i-1]]
            if v_prev>VIXMAX: continue
            cl=[ohlc[days[j]][3] for j in range(i-20,i)]
            sig=st.pstdev([math.log(cl[j]/cl[j-1]) for j in range(1,20)])
            Sp=o; K=Sp*(1-max(FLOOR,2.0*sig)); W=K*(1-WIDTH); width=K-W; iv0=v_prev/100
            credit=put(Sp,K,iv0,1.9/252)*0.97-put(Sp,W,iv0,1.9/252)*1.8*1.03
            if credit<0.10*Sp/730: continue
            m=mf(i); cut+= m<0.05
            qty=int(m*NLV/((width-credit)*100))
            if qty<1: continue
            n+=1; settle=max(K-c1,0)-max(W-c1,0); p=(credit-settle)*100*qty-COMM*2*qty
            pnl+=p; eq+=p; peak=max(peak,eq); dd=max(dd,peak-eq)
        yrs=max(len(idx)/252,1e-9); out[name]=(pnl/NLV*100/yrs, dd/NLV*100, cut)
    return out
WINDOWS=[("all","2005-11-01","2026-11-27"),("gfc08","2008-09-01","2008-12-31"),("volm18","2018-01-20","2018-03-10"),("q4-18","2018-10-01","2018-12-31"),("covid","2020-02-15","2020-04-30"),("bear22","2022-01-01","2022-12-31"),("tariff25","2025-03-01","2025-04-30"),("bull21","2021-01-01","2021-12-31"),("nov26","2026-11-09","2026-11-27")]
R={w:run(a,b) for w,a,b in WINDOWS}
print(f"{'rule':24s}"+"".join(f"{w:>8s}" for w in R)+"  | maxDD  days cut")
for name in V:
    print(f"{name:24s}"+"".join(f"{R[w][name][0]:8.1f}" for w in R)+f"  | {R['all'][name][1]:5.1f} {R['all'][name][2]:6d}")
