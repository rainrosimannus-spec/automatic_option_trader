"""Options-universe filter (Rain, 2026-11-11): pick the ≤50 names the wheel may sell puts on
from the screener's options_universe.yaml — WITHOUT touching the screener.

Why: the account's $92k of stock losses came from names that gapped 7-10% in non-earnings
weeks and never came back (IREN −60%, LRCX −45%, FTAI −30%, CIEN −25%). Rain's design holds an
assigned lot and works it out through net-basis calls, which only works on companies he would
own for 10-15 years. So the put universe must be the compounder's monthly 100, minus the
early-stage tier, minus anything whose own price history shows gap risk, minus anything a
single lot of which exceeds the one-lot ceiling.

Pure functions here (metrics, judge, select) are shared by the live loader
(UniverseManager), the preview script and any sweep. FMP history is cached per day in a JSON
file so the audit (kept / dropped / why) is readable. Fail-open on missing history: a data
hiccup never empties the universe — the name is kept and flagged `no_history`.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from src.core.logger import get_logger

log = get_logger(__name__)


# ── pure metrics ─────────────────────────────────────────────────────────────
def compute_metrics(closes: list[float], breach_lookback: int = 120, breach_horizon: int = 3,
                    breach_threshold: float = 0.025) -> Optional[dict]:
    """Trailing risk metrics from daily closes (oldest→newest). None when < 60 bars."""
    if not closes or len(closes) < 60:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 40:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    ann_vol = math.sqrt(var) * math.sqrt(252)
    peak = closes[0]; max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        max_dd = max(max_dd, 1.0 - c / peak if peak > 0 else 0.0)
    worst_day = min(rets)
    from src.strategy.realized_move import breach_rate
    br = breach_rate(closes, lookback=breach_lookback, horizon=breach_horizon, threshold=breach_threshold)
    return {"ann_vol": round(ann_vol, 4), "max_dd_1y": round(max_dd, 4), "worst_day": round(worst_day, 4),
            "breach_rate": (round(br, 3) if br is not None else None), "last_close": round(closes[-1], 2),
            "bars": len(closes)}


def judge(entry: dict, metrics: Optional[dict], cfg) -> tuple[bool, list[str]]:
    """(keep, reasons). Reasons list every rule the name fails; empty → kept.
    Missing metrics → kept with the single flag 'no_history' (fail-open)."""
    reasons: list[str] = []
    tier = str(entry.get("tier", "growth"))
    if tier in set(cfg.exclude_tiers or []):
        reasons.append(f"tier:{tier}")
    liq = entry.get("options_liquidity")
    if liq is not None and float(liq) < float(cfg.min_liquidity):
        reasons.append(f"liquidity:{float(liq):.0f}<{cfg.min_liquidity:.0f}")
    if metrics is None:
        return (not reasons), (reasons or ["no_history"])
    if metrics["ann_vol"] > cfg.max_ann_vol:
        reasons.append(f"vol:{metrics['ann_vol']:.0%}>{cfg.max_ann_vol:.0%}")
    if metrics["max_dd_1y"] > cfg.max_drawdown_1y:
        reasons.append(f"dd1y:{metrics['max_dd_1y']:.0%}>{cfg.max_drawdown_1y:.0%}")
    if metrics["worst_day"] < cfg.worst_day_floor:
        reasons.append(f"gap:{metrics['worst_day']:.1%}<{cfg.worst_day_floor:.0%}")
    if metrics["breach_rate"] is not None and metrics["breach_rate"] > cfg.max_breach_rate:
        reasons.append(f"breach:{metrics['breach_rate']:.0%}>{cfg.max_breach_rate:.0%}")
    if cfg.max_share_price and metrics["last_close"] > cfg.max_share_price:
        reasons.append(f"price:{metrics['last_close']:.0f}>{cfg.max_share_price:.0f}")
    return (not reasons), reasons


def select_options_universe(entries: list[dict], metrics_by_symbol: dict, cfg
                            ) -> tuple[list[dict], list[tuple[dict, list[str]]]]:
    """Apply judge() to every entry, rank the survivors by options_score (desc, then rank asc),
    cut to cfg.max_names. Returns (kept, dropped-with-reasons)."""
    kept, dropped = [], []
    for e in entries:
        ok, why = judge(e, metrics_by_symbol.get(str(e.get("symbol"))), cfg)
        if ok:
            kept.append(e)
        else:
            dropped.append((e, why))
    kept.sort(key=lambda e: (-float(e.get("options_score") or 0), int(e.get("rank") or 9999)))
    cut = kept[cfg.max_names:]
    for e in cut:
        dropped.append((e, [f"rank>{cfg.max_names}"]))
    return kept[:cfg.max_names], dropped


# ── data (FMP, daily file cache) ─────────────────────────────────────────────
def _fetch_closes(symbol: str, days: int) -> Optional[list[float]]:
    """Own FMP call (not get_price_history: its per-symbol cache ignores `days`)."""
    try:
        from src.portfolio.fmp import _get
        start = (date.today() - timedelta(days=days)).isoformat()
        data = _get("historical-price-eod/full", symbol, {"from": start, "to": date.today().isoformat()})
        if not data or not isinstance(data, list):
            return None
        closes = [float(r["close"]) for r in reversed(data) if isinstance(r, dict) and r.get("close") is not None]
        return closes if len(closes) >= 2 else None
    except Exception as e:
        log.warning("universe_filter_history_failed", symbol=symbol, error=str(e))
        return None


def build_metrics(symbols: list[str], cfg, today: Optional[str] = None) -> dict:
    """{symbol: metrics|None} — cached per day in cfg.cache_path; only missing symbols are fetched."""
    today = today or date.today().isoformat()
    path = Path(cfg.cache_path)
    cache: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if raw.get("date") == today:
                cache = raw.get("metrics", {})
        except Exception:
            cache = {}
    missing = [s for s in symbols if s not in cache]
    for s in missing:
        closes = _fetch_closes(s, int(cfg.history_days))
        cache[s] = compute_metrics(closes) if closes else None
    if missing:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"date": today, "metrics": cache}, indent=1))
        except Exception as e:
            log.warning("universe_filter_cache_write_failed", error=str(e))
    return {s: cache.get(s) for s in symbols}


def load_raw_options_universe() -> list[dict]:
    import yaml
    from src.core.config import _CONFIG_DIR
    p = _CONFIG_DIR / "options_universe.yaml"
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    return [dict(x) for x in raw.get("stocks", []) if isinstance(x, dict) and x.get("symbol")]


def filtered_symbols(cfg) -> Optional[tuple[set[str], list[tuple[dict, list[str]]]]]:
    """Kept-symbol set + dropped list for the live loader. None when the filter is off or the
    screener file is missing (caller keeps the unfiltered universe)."""
    if not cfg.enabled:
        return None
    entries = load_raw_options_universe()
    if not entries:
        return None
    metrics = build_metrics([str(e["symbol"]) for e in entries], cfg)
    kept, dropped = select_options_universe(entries, metrics, cfg)
    return {str(e["symbol"]) for e in kept}, dropped
