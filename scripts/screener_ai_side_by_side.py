#!/usr/bin/env python
"""Screener AI side-by-side — run the breakthrough SCAN under two model profiles and compare.

WHY
  Changing the model behind the screener changes which companies get picked. Before the default
  profile in tools/screen_universe.py (SCREENER_AI_PROFILE) is flipped, run the current and the
  candidate profile on the SAME prompt and read both lists.

WHAT IT DOES (read-only: no IBKR, no database, no watchlist or universe file is touched)
  1. Builds the real breakthrough-scan prompt (same exclusions as the monthly screen).
  2. Calls the scan once per profile.
  3. Looks every proposed ticker up at FMP: does it exist, is the company the one the model
     named, market cap, listing date, ETF flag. A name is marked:
        ok            exists, right company, inside the tier's size rules
        recent        listed in the last 18 months (the blind spot web search is meant to fix)
        too_big       market cap above $200B (10x would exceed the tier's $2T ceiling)
        too_small     market cap under the $500M floor
        mismatch      FMP has a different company under that ticker
        unknown       no FMP profile (expected for most non-US tickers — the live screen
                      qualifies those at IBKR instead, so this is NOT a failure)
  4. Writes data/screener_ai_side_by_side.json and prints the comparison.

  Stage B/C of the live pipeline (IBKR qualification, anchored selection of the final 25) are
  NOT run — this compares what each model PROPOSES, which is where the two differ.

USAGE
  python scripts/screener_ai_side_by_side.py [--profiles opus-4-8 opus-5-5-search]
Costs real API money: one scan per profile (a few dollars at most).
"""
import argparse, json, os, sys, time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.chdir(ROOT)

import screen_universe as su  # noqa: E402

OUT = "data/screener_ai_side_by_side.json"


def _fmp_check(c: dict) -> dict:
    sym = str(c.get("symbol") or "")
    usd = (c.get("currency") or "USD").upper() == "USD"
    prof = su._fmp_get("profile", sym) or []
    p = prof[0] if isinstance(prof, list) and prof else {}
    if not p:
        return {"verdict": "unknown", "note": "no FMP profile" + ("" if usd else " (non-US ticker)")}
    name, cap, ipo = p.get("companyName") or "", p.get("marketCap") or p.get("mktCap") or 0, p.get("ipoDate") or ""
    out = {"fmp_name": name, "market_cap_bn": round(cap / 1e9, 1) if cap else None, "ipo_date": ipo}
    if not usd and not su._same_company(c.get("name") or "", name):
        return {**out, "verdict": "unknown", "note": "non-US ticker; FMP shows a different US company"}
    if usd and not _names_agree(c.get("name") or "", name):
        return {**out, "verdict": "mismatch", "note": f"model said '{c.get('name')}', FMP has '{name}'"}
    if p.get("isEtf") or p.get("isFund"):
        return {**out, "verdict": "mismatch", "note": "ETF / fund"}
    if cap and cap < 500e6:
        return {**out, "verdict": "too_small", "note": "under the $500M floor"}
    if cap and cap > 200e9:
        return {**out, "verdict": "too_big", "note": "10x would exceed $2T"}
    if ipo and ipo >= (datetime.utcnow() - timedelta(days=548)).strftime("%Y-%m-%d"):
        return {**out, "verdict": "recent", "note": f"listed {ipo}"}
    return {**out, "verdict": "ok", "note": ""}


def _names_agree(a: str, b: str) -> bool:
    """Looser than the screener's _same_company (which compares two IBKR strings): the model
    writes 'Rocket Lab', FMP 'Rocket Lab USA, Inc.'. Same first significant word is enough to
    catch a wrong-ticker conflation, which is all this check is for."""
    ta, tb = su._company_tokens(a), su._company_tokens(b)
    return bool(ta and tb and (ta[0] == tb[0] or ta[0].startswith(tb[0]) or tb[0].startswith(ta[0])))


def run_profile(profile: str, prompt: str) -> dict:
    print(f"\n=== {profile}: scanning … ({datetime.utcnow():%H:%M:%S} UTC)")
    t0 = time.time()
    try:
        text = su._anthropic_messages(prompt, max_tokens=24000, label=f"scan[{profile}]",
                                      web_search=True, profile=profile).strip()
    except Exception as e:
        print(f"  ❌ {profile}: {type(e).__name__}: {e}")
        return {"profile": profile, "error": f"{type(e).__name__}: {e}", "candidates": []}
    call = dict(su.LAST_AI_CALL)
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    cands = [c for c in su._loads_json_array_salvage(text) if isinstance(c, dict) and c.get("symbol")]
    for c in cands:
        c["symbol"] = su.canonical_symbol(str(c["symbol"]))
        c["check"] = _fmp_check(c)
        time.sleep(0.15)
    print(f"  {len(cands)} names in {time.time() - t0:.0f}s | served by {call.get('model')} | "
          f"searches {call.get('searches')} | tokens in/out {call.get('input_tokens')}/{call.get('output_tokens')}")
    return {"profile": profile, "call": call, "candidates": cands}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", nargs="+", default=["opus-4-8", "opus-5-5-search"])
    args = ap.parse_args()
    prompt = su._build_breakthrough_prompt()
    runs = [run_profile(p, prompt) for p in args.profiles]
    json.dump({"run_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "runs": runs},
              open(OUT, "w"), indent=1, default=str)

    sets = {r["profile"]: {c["symbol"] for c in r["candidates"]} for r in runs}
    for r in runs:
        print(f"\n── {r['profile']} — {len(r['candidates'])} proposed" + (f" — ERROR {r['error']}" if r.get("error") else ""))
        others = set().union(*(s for p, s in sets.items() if p != r["profile"])) if len(sets) > 1 else set()
        for c in r["candidates"]:
            k = c["check"]
            only = "" if c["symbol"] in others else "  ◀ only here"
            print(f"  {c['symbol']:8s} {str(c.get('currency') or ''):4s} {k['verdict']:9s} "
                  f"{(str(k.get('market_cap_bn')) + 'B') if k.get('market_cap_bn') else '':>9s}  "
                  f"{(c.get('name') or '')[:30]:30s} | {(c.get('megatrend') or '')[:26]:26s}{only}"
                  + (f"  [{k['note']}]" if k.get("note") and k["verdict"] not in ("ok", "unknown") else ""))
        tally = {}
        for c in r["candidates"]:
            tally[c["check"]["verdict"]] = tally.get(c["check"]["verdict"], 0) + 1
        print(f"  verdicts: {tally}")
    if len(runs) == 2:
        a, b = (r["profile"] for r in runs)
        print(f"\nOverlap: {len(sets[a] & sets[b])} names in both | only {a}: {len(sets[a] - sets[b])} | only {b}: {len(sets[b] - sets[a])}")
    print(f"\nSaved {OUT}")


if __name__ == "__main__":
    main()
