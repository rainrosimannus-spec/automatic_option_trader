"""
Screener route — web UI for the annual universe refresh tool.
Lets users trigger a screen, view ranked results, and approve watchlist changes.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import HTMLResponse

from src.web.template_engine import templates
from src.core.logger import get_logger

log = get_logger(__name__)

router = APIRouter()

# ── Screening state (in-memory, single user) ────────────────
_screen_state = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_region": "",
    "current_symbol": "",
    "results": [],          # list of dicts
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _run_screen(regions: list[str] | None, min_mcap: float, top_n: int):
    """Background thread: run the screener and update state."""
    global _screen_state

    _screen_state.update({
        "running": True,
        "progress": 0,
        "total": 0,
        "results": [],
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "error": None,
    })

    try:
        # Ensure event loop
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        from ib_insync import IB
        from tools.screen_universe import UniverseScreener, CANDIDATE_POOLS

        # Count total candidates
        pools_to_scan = {k: v for k, v in CANDIDATE_POOLS.items()
                         if regions is None or k in regions}
        total = sum(len(p["symbols"]) for p in pools_to_scan.values())
        _screen_state["total"] = total

        # Connect with different client ID — use options trader's TWS
        from src.core.config import get_settings
        cfg = get_settings().ibkr
        ib = IB()
        ib.connect(cfg.host, cfg.port, clientId=3)
        log.info("screener_connected", accounts=ib.managedAccounts())

        screener = UniverseScreener(ib)
        all_scores = []
        progress = 0

        for region, pool in pools_to_scan.items():
            _screen_state["current_region"] = region

            for symbol in pool["symbols"]:
                _screen_state["current_symbol"] = str(symbol)
                progress += 1
                _screen_state["progress"] = progress

                try:
                    score = screener._score_stock(
                        symbol=str(symbol),
                        exchange=pool["exchange"],
                        currency=pool["currency"],
                    )
                    if score and score.market_cap >= min_mcap:
                        all_scores.append({
                            "symbol": score.symbol,
                            "name": score.name,
                            "exchange": score.exchange,
                            "currency": score.currency,
                            "sector": score.sector,
                            "price": round(score.price, 2),
                            "market_cap_b": round(score.market_cap / 1e9, 1),
                            "composite_score": round(score.composite_score, 1),
                            "growth_score": round(score.growth_score, 1),
                            "valuation_score": round(score.valuation_score, 1),
                            "quality_score": round(score.quality_score, 1),
                            "options_liquidity": round(score.options_liquidity, 1),
                            "options_available": score.options_available,
                            "dividend_yield": round(score.dividend_yield, 1),
                            "category": "dividend" if score.dividend_yield > 2.5 else "growth",
                            "selected": False,
                        })
                except Exception as e:
                    log.debug("screen_stock_error", symbol=symbol, error=str(e))

                time.sleep(0.5)  # rate limit

        # Sort by composite score
        all_scores.sort(key=lambda s: s["composite_score"], reverse=True)

        # Auto-select top N
        growth = [s for s in all_scores if s["category"] == "growth"]
        dividend = [s for s in all_scores if s["category"] == "dividend"]

        for s in growth[:40]:
            s["selected"] = True
        for s in dividend[:10]:
            s["selected"] = True

        _screen_state["results"] = all_scores
        _screen_state["finished_at"] = datetime.now().isoformat()

        ib.disconnect()
        log.info("screener_finished", total_scored=len(all_scores))

    except Exception as e:
        _screen_state["error"] = str(e)
        log.error("screener_error", error=str(e))
    finally:
        _screen_state["running"] = False


@router.get("/screener", response_class=HTMLResponse)
async def screener_page(request: Request):
    """Main screener page."""
    return templates.TemplateResponse("screener.html", {
        "request": request,
        "state": _screen_state,
    })


@router.post("/screener/start")
async def start_screen(request: Request):
    """Trigger a new screening run."""
    if _screen_state["running"]:
        return HTMLResponse(
            '<div class="text-amber-400">⚠ Screen already in progress</div>'
        )

    form = await request.form()
    regions_str = form.get("regions", "")
    regions = [r.strip() for r in regions_str.split(",") if r.strip()] or None
    min_mcap = float(form.get("min_mcap", 5e9))
    top_n = int(form.get("top_n", 50))

    thread = threading.Thread(
        target=_run_screen,
        args=(regions, min_mcap, top_n),
        daemon=True,
    )
    thread.start()

    return HTMLResponse(
        '<div class="text-emerald-400">🔍 Screening started...</div>'
    )


@router.get("/screener/progress")
async def screen_progress():
    """HTMX polling endpoint for screening progress."""
    s = _screen_state

    if not s["running"] and not s["results"]:
        return HTMLResponse(
            '<div class="text-gray-500">No screen running. Click "Start Screen" to begin.</div>'
        )

    if s["running"]:
        pct = int(s["progress"] / s["total"] * 100) if s["total"] > 0 else 0
        return HTMLResponse(f"""
        <div class="space-y-3">
            <div class="flex items-center gap-3">
                <div class="animate-spin h-5 w-5 border-2 border-emerald-400 border-t-transparent rounded-full"></div>
                <span class="text-emerald-400 font-medium">Screening in progress...</span>
            </div>
            <div class="text-sm text-gray-400">
                Region: <span class="text-white">{s['current_region']}</span> |
                Symbol: <span class="text-white">{s['current_symbol']}</span> |
                Progress: <span class="text-white">{s['progress']}/{s['total']}</span>
            </div>
            <div class="w-full bg-gray-800 rounded-full h-2.5">
                <div class="bg-emerald-500 h-2.5 rounded-full transition-all" style="width: {pct}%"></div>
            </div>
            <div class="text-xs text-gray-500">{len([r for r in s['results'] if r.get('composite_score', 0) > 0])} candidates scored so far</div>
        </div>
        """)

    if s["error"]:
        return HTMLResponse(f'<div class="text-red-400">❌ Error: {s["error"]}</div>')

    # Finished — show results count
    selected = len([r for r in s["results"] if r["selected"]])
    return HTMLResponse(f"""
    <div class="text-emerald-400">
        ✅ Screening complete — {len(s['results'])} stocks scored, {selected} selected
    </div>
    """)


@router.get("/screener/results")
async def screen_results():
    """Return the results table as HTML."""
    results = _screen_state.get("results", [])
    if not results:
        return HTMLResponse('<div class="text-gray-500">No results yet.</div>')

    rows = []
    for i, s in enumerate(results):
        selected_cls = "bg-emerald-950/30 border-l-2 border-emerald-500" if s["selected"] else "border-l-2 border-transparent"
        opts_badge = '<span class="text-emerald-400 text-xs">✓ Options</span>' if s["options_available"] else '<span class="text-red-400 text-xs">✗ No opts</span>'
        cat_badge = f'<span class="px-1.5 py-0.5 rounded text-xs {"bg-blue-900/50 text-blue-300" if s["category"] == "growth" else "bg-amber-900/50 text-amber-300"}">{s["category"]}</span>'

        rows.append(f"""
        <tr class="{selected_cls} hover:bg-gray-800/50 transition">
            <td class="px-3 py-2 text-center">
                <input type="checkbox" name="selected" value="{s['symbol']}"
                       {"checked" if s["selected"] else ""}
                       class="rounded bg-gray-800 border-gray-600 text-emerald-500 focus:ring-emerald-500">
            </td>
            <td class="px-3 py-2 text-sm font-medium text-white">{i+1}</td>
            <td class="px-3 py-2">
                <div class="font-medium text-white">{s['symbol']}</div>
                <div class="text-xs text-gray-500">{s['name'][:30]}</div>
            </td>
            <td class="px-3 py-2 text-xs text-gray-400">{s['exchange']}</td>
            <td class="px-3 py-2 text-xs text-gray-400">{s['currency']}</td>
            <td class="px-3 py-2 text-sm font-bold text-emerald-400">{s['composite_score']}</td>
            <td class="px-3 py-2 text-sm text-gray-300">{s['growth_score']}</td>
            <td class="px-3 py-2 text-sm text-gray-300">{s['valuation_score']}</td>
            <td class="px-3 py-2 text-sm text-gray-300">{s['quality_score']}</td>
            <td class="px-3 py-2 text-sm text-gray-300">{s['options_liquidity']}</td>
            <td class="px-3 py-2">{opts_badge}</td>
            <td class="px-3 py-2 text-sm text-gray-300">{s['dividend_yield']}%</td>
            <td class="px-3 py-2">{cat_badge}</td>
            <td class="px-3 py-2 text-sm text-gray-400">${s['market_cap_b']}B</td>
        </tr>
        """)

    return HTMLResponse(f"""
    <div class="overflow-x-auto">
        <table class="w-full text-left">
            <thead class="text-xs text-gray-500 uppercase border-b border-gray-800">
                <tr>
                    <th class="px-3 py-2 w-8">✓</th>
                    <th class="px-3 py-2 w-8">#</th>
                    <th class="px-3 py-2">Stock</th>
                    <th class="px-3 py-2">Exchange</th>
                    <th class="px-3 py-2">CCY</th>
                    <th class="px-3 py-2">Score</th>
                    <th class="px-3 py-2">Growth</th>
                    <th class="px-3 py-2">Value</th>
                    <th class="px-3 py-2">Quality</th>
                    <th class="px-3 py-2">OptLiq</th>
                    <th class="px-3 py-2">Opts</th>
                    <th class="px-3 py-2">Div%</th>
                    <th class="px-3 py-2">Type</th>
                    <th class="px-3 py-2">MCap</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-800/50">
                {"".join(rows)}
            </tbody>
        </table>
    </div>
    """)


@router.post("/screener/apply")
async def apply_watchlist(request: Request):
    """Apply selected stocks as the new watchlist."""
    form = await request.form()
    selected_symbols = form.getlist("selected")

    if not selected_symbols:
        return HTMLResponse('<div class="text-red-400">No stocks selected.</div>')

    results = _screen_state.get("results", [])
    results_map = {r["symbol"]: r for r in results}

    # Build new watchlist
    import yaml

    growth_entries = []
    dividend_entries = []

    for sym in selected_symbols:
        r = results_map.get(sym)
        if not r:
            continue

        entry = {
            "symbol": r["symbol"],
            "name": r["name"],
            "sector": r["sector"],
            "exchange": r["exchange"],
            "currency": r["currency"],
        }
        if r["currency"] == "JPY":
            entry["contract_size"] = 100
        if r["dividend_yield"] > 0:
            entry["div_yield"] = r["dividend_yield"]

        if r["category"] == "dividend":
            entry["category"] = "dividend"
            dividend_entries.append(entry)
        else:
            entry["category"] = "growth"
            growth_entries.append(entry)

    all_entries = growth_entries + dividend_entries

    # Write new watchlist
    watchlist_path = Path("config/watchlist.yaml")
    backup_path = Path(f"config/watchlist_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml")

    # Backup current watchlist
    if watchlist_path.exists():
        import shutil
        shutil.copy(watchlist_path, backup_path)

    # Write YAML
    header = f"# Watchlist generated {datetime.now().strftime('%Y-%m-%d %H:%M')} via dashboard screener\n"
    header += f"# {len(growth_entries)} growth + {len(dividend_entries)} dividend = {len(all_entries)} total\n"
    header += f"# Previous watchlist backed up to {backup_path.name}\n\n"

    with open(watchlist_path, "w") as f:
        f.write(header)
        yaml.dump(
            {"stocks": all_entries},
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    log.info(
        "watchlist_updated",
        growth=len(growth_entries),
        dividend=len(dividend_entries),
        total=len(all_entries),
        backup=backup_path.name,
    )

    return HTMLResponse(f"""
    <div class="bg-emerald-900/30 border border-emerald-700 rounded-lg p-4 space-y-2">
        <div class="text-emerald-400 font-medium">✅ Watchlist updated!</div>
        <div class="text-sm text-gray-300">
            {len(growth_entries)} growth + {len(dividend_entries)} dividend = {len(all_entries)} stocks
        </div>
        <div class="text-xs text-gray-500">
            Previous watchlist backed up to {backup_path.name}
        </div>
        <div class="text-sm text-amber-400 mt-2">
            ⚠ Restart the trader to activate the new watchlist.
        </div>
    </div>
    """)
