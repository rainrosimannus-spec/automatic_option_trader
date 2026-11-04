#!/usr/bin/env python
"""Put the universe back to the state a screen found it in.

    .venv/bin/python scripts/screener_restore.py            # list archived runs
    .venv/bin/python scripts/screener_restore.py latest     # show what the newest run's snapshot holds
    .venv/bin/python scripts/screener_restore.py 20261104-052037 --apply

--apply copies the snapshot files back (screened/options universe, pool, durability index,
structural risks, screener run log) and replaces every portfolio_watchlist row with the
snapshot's rows. Holdings are never touched. Restart the trader afterwards so its cached
options universe is reloaded. Without --apply nothing is written.
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.portfolio import screener_runs as sr  # noqa: E402


def _pick(arg: str) -> Path:
    runs = sr.list_runs()
    if not runs:
        sys.exit("no archived runs under data/screener_runs")
    if arg == "latest":
        return runs[-1]
    for p in runs:
        if p.name == arg:
            return p
    sys.exit(f"no run folder named {arg}; known: {', '.join(p.name for p in runs)}")


def _restore_watchlist(rows: list[dict]) -> int:
    from src.core.database import get_db
    from src.portfolio.models import PortfolioWatchlist
    cols = {c.name: c for c in PortfolioWatchlist.__table__.columns}
    with get_db() as db:
        db.query(PortfolioWatchlist).delete()
        for row in rows:
            kwargs = {}
            for k, v in row.items():
                if k not in cols:
                    continue
                try:
                    is_dt = cols[k].type.python_type is datetime
                except NotImplementedError:
                    is_dt = False
                if v is not None and is_dt and isinstance(v, str):
                    v = datetime.fromisoformat(v)
                kwargs[k] = v
            db.add(PortfolioWatchlist(**kwargs))
        db.commit()
    return len(rows)


def main(argv: list[str]) -> None:
    if not argv:
        for p in sr.list_runs():
            st = sr.read_status(p)
            print(f"{p.name}  {st.get('status', '?'):8s}  rows={st.get('watchlist_rows', '?')}  "
                  f"files={len(st.get('snapshot_files', []))}  {st.get('error', '')[:80]}")
        return
    run_dir = _pick(argv[0])
    before = run_dir / "before"
    st = sr.read_status(run_dir)
    rows = json.loads((before / "watchlist.json").read_text())
    print(f"run {run_dir.name}: status={st.get('status')} started={st.get('started_at')}")
    print(f"snapshot: {len(rows)} watchlist rows, files: {', '.join(st.get('snapshot_files', []))}")
    tiers = {}
    for r in rows:
        tiers[r.get("tier")] = tiers.get(r.get("tier"), 0) + 1
    print(f"tiers in snapshot: {tiers}; pending_removal={sum(1 for r in rows if r.get('pending_removal'))}")
    if "--apply" not in argv:
        print("dry run — add --apply to restore")
        return
    for rel in st.get("snapshot_files", []):
        src = before / Path(rel).name
        if src.is_file():
            Path(rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, rel)
            print(f"restored {rel}")
    n = _restore_watchlist(rows)
    print(f"restored {n} watchlist rows — restart the trader now (! deploy/restart-trader.sh)")


if __name__ == "__main__":
    main(sys.argv[1:])
