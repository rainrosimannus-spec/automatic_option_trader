"""Per-run archive of the monthly screen (Rain, 2026-11-04).

Two failures taught what is missing when a screen goes wrong:
  * 2026-11-02 — a run marked "success" wrote a broken 32-name universe over the healthy one.
    The state from BEFORE the run (watchlist rows with their scores, options universe, pool
    file) was gone; the hand restore had to leave every score at zero.
  * 2026-11-03 — a run failed cleanly (collapse guard) and wrote nothing; its scored table
    existed only in the console log and had to be rebuilt by hand for the analysis.

So every run gets a folder under data/screener_runs/<started-at>/:
  before/          snapshot of the universe files + the watchlist table, taken before the screen
                   touches anything (the restore source — scripts/screener_restore.py)
  results.json     the run's full scoring table, selections, rejects and AI outcomes, written as
                   soon as the screener returns, success or not
  status.json      running → success | error | retry

Retention: a SUCCESSFUL run deletes every other folder except the newest earlier success, so a
bad write can still be undone one run later; failed runs keep their folder until the next
success. Nothing here changes what the screener decides; every call is wrapped so a failure in
the archive never stops a screen.
"""
from __future__ import annotations

import dataclasses
import json
import math
import shutil
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from src.core.logger import get_logger

log = get_logger(__name__)

RUNS_DIR = Path("data/screener_runs")
SNAPSHOT_FILES = (
    "config/screened_universe.yaml",
    "config/options_universe.yaml",
    "tools/discovered_pool.yaml",
    "config/durability_index.yaml",
    "config/structural_risks.yaml",
    "data/screener_last_run.json",
)
KEEP_PREVIOUS_SUCCESSES = 1


def _clean(v):
    """JSON-safe copy: dataclasses → dicts, datetimes → ISO strings, NaN/inf → null."""
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _clean(dataclasses.asdict(v))
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_clean(x) for x in (sorted(v) if isinstance(v, (set, frozenset)) else v)]
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if v is None or isinstance(v, (bool, int, str)):
        return v
    return str(v)


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_clean(obj), indent=1))
    tmp.replace(path)


def _dump_watchlist() -> list[dict]:
    """Every row of portfolio_watchlist, every column (the restore re-inserts them as they were)."""
    from src.core.database import get_db
    from src.portfolio.models import PortfolioWatchlist
    cols = [c.name for c in PortfolioWatchlist.__table__.columns]
    with get_db() as db:
        return [{c: getattr(r, c) for c in cols} for r in db.query(PortfolioWatchlist).all()]


def read_status(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "status.json").read_text())
    except Exception:
        return {}


def list_runs() -> list[Path]:
    """Run folders, oldest first."""
    if not RUNS_DIR.is_dir():
        return []
    return sorted(p for p in RUNS_DIR.iterdir() if p.is_dir())


def start_run(now: Optional[datetime] = None) -> Path:
    """Create the run folder and take the before-snapshot. Call BEFORE the screen writes."""
    now = now or datetime.utcnow()
    run_dir = RUNS_DIR / now.strftime("%Y%m%d-%H%M%S")
    before = run_dir / "before"
    before.mkdir(parents=True, exist_ok=True)
    gi = RUNS_DIR / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    copied = []
    for rel in SNAPSHOT_FILES:
        src = Path(rel)
        if src.is_file():
            shutil.copy2(src, before / src.name)
            copied.append(rel)
    rows = _dump_watchlist()
    _write_json(before / "watchlist.json", rows)
    _write_json(run_dir / "status.json", {
        "status": "running", "started_at": now.isoformat(), "snapshot_files": copied,
        "watchlist_rows": len(rows),
    })
    log.info("screener_run_archive_started", run_dir=str(run_dir), files=len(copied),
             watchlist_rows=len(rows))
    return run_dir


def save_results(run_dir: Path, screener, portfolio_universe, options_universe, all_scores) -> None:
    """The run's scoring table and decisions — written as soon as the screener returns, before
    any guard, so a run the guard aborts still leaves its numbers behind."""
    _write_json(run_dir / "results.json", {
        "saved_at": datetime.utcnow().isoformat(),
        "selected": [
            {"symbol": s.symbol, "tier": s.tier, "portfolio_score": s.portfolio_score,
             "durability_pick": getattr(s, "durability_pick", False)}
            for s in portfolio_universe
        ],
        "options_universe": [getattr(s, "symbol", s) for s in options_universe],
        "scores": list(all_scores),
        "rejects": getattr(screener, "_rejects", []),
        "reject_counts": getattr(screener, "_reject_counts", {}),
        "breakthrough_proposed": getattr(screener, "_breakthrough_proposed", []),
        "breakthrough_graduated": getattr(screener, "_breakthrough_graduated", []),
        "durability": getattr(screener, "_durability", {}),
    })
    log.info("screener_run_archive_results_saved", run_dir=str(run_dir),
             selected=len(portfolio_universe), scored=len(all_scores))


def finish_run(run_dir: Path, status: str, error: Optional[str] = None) -> None:
    st = read_status(run_dir)
    st.update({"status": status, "finished_at": datetime.utcnow().isoformat()})
    if error:
        st["error"] = error
    _write_json(run_dir / "status.json", st)
    if status == "success":
        prune(keep=run_dir)


def prune(keep: Path) -> list[Path]:
    """After a success: keep this run and the newest earlier success, delete the rest."""
    others = [p for p in list_runs() if p.resolve() != keep.resolve()]
    earlier_successes = [p for p in others if read_status(p).get("status") == "success"]
    protected = set(p.resolve() for p in earlier_successes[-KEEP_PREVIOUS_SUCCESSES:]) if KEEP_PREVIOUS_SUCCESSES else set()
    removed = []
    for p in others:
        if p.resolve() in protected:
            continue
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p)
    if removed:
        log.info("screener_run_archive_pruned", removed=[p.name for p in removed],
                 kept=[p.name for p in list_runs()])
    return removed
