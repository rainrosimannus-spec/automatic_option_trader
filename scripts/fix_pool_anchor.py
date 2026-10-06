"""Undo what the dead 2026-10-05 screen wrote to tools/discovered_pool.yaml.

It re-stamped 23 breakthrough entries with last_run_at 2026-10-05T23:37:47 (and last_seen
2026-10, appearance_count +1) although it had scored none of them, and that stamp is what
_load_breakthrough_anchor() uses as the anchor. This puts those 23 back on the September
anchor (2026-09-07T23:39:27), so the retry starts from the same 25-name anchor the Monday run
should have had (GEV and SRPT included). Nothing else in the file is touched.
Run from the repo root:  .venv/bin/python scripts/fix_pool_anchor.py  (or in Claude Code: ! .venv/bin/python scripts/fix_pool_anchor.py)
"""
import sys, yaml
sys.path.insert(0, "tools")
p = "tools/discovered_pool.yaml"
DEAD, PREV = "2026-10-05T23:37:47", "2026-09-07T23:39:27"
cur = yaml.safe_load(open(p))
n = 0
for e in cur["breakthrough"]:
    if e.get("last_run_at") == DEAD:
        e["last_run_at"], e["last_seen"] = PREV, "2026-09"
        e["appearance_count"] = max(1, (e.get("appearance_count") or 1) - 1)
        n += 1
with open(p, "w") as f:
    yaml.safe_dump(cur, f, sort_keys=False, default_flow_style=False)
import screen_universe as su
anchor = sorted(e["symbol"] for e in su._load_breakthrough_anchor())
print(f"reverted {n} entries; anchor now {len(anchor)} names: {anchor}")
