"""
Nightly Chronos price forecast job for Winston's watchlist.

Runs at 17:30 ET (after US close, before Asian open).
Fetches 6 months of daily prices per watchlist stock via IBKR,
runs Chronos small model, writes results to portfolio_forecasts table.

Winston's buyer reads this table — if trend is DOWN with high confidence,
entry is delayed by one scan cycle.
"""
from __future__ import annotations

import numpy as np
from datetime import datetime, date
from src.core.logger import get_logger
from src.core.database import get_db
from src.portfolio.models import PortfolioWatchlist, PortfolioForecast

log = get_logger(__name__)

_chronos_pipeline = None


def _get_pipeline():
    """Load Chronos model once and cache it."""
    global _chronos_pipeline
    if _chronos_pipeline is not None:
        return _chronos_pipeline
    try:
        import torch
        from chronos import BaseChronosPipeline
        log.info("chronos_loading")
        _chronos_pipeline = BaseChronosPipeline.from_pretrained(
            "amazon/chronos-t5-small",
            device_map="cpu",
            dtype=torch.float32,
        )
        log.info("chronos_loaded")
    except Exception as e:
        log.error("chronos_load_failed", error=str(e))
        raise
    return _chronos_pipeline


def _fetch_prices(ib, symbol: str, exchange: str, currency: str) -> np.ndarray | None:
    """Fetch 6 months of daily closing prices from IBKR."""
    try:
        from ib_insync import Stock
        from src.portfolio.connection import get_portfolio_lock
        from src.portfolio.symbols import broker_stock
        contract = broker_stock(symbol, exchange, currency)   # internal name -> broker contract
        with get_portfolio_lock():
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="180 D",
                barSizeSetting="1 day",
            whatToShow="CLOSE",
            useRTH=True,
            timeout=10,
        )
        if not bars or len(bars) < 30:
            return None
        return np.array([b.close for b in bars], dtype=np.float32)
    except Exception as e:
        log.warning("chronos_price_fetch_failed", symbol=symbol, error=str(e))
        return None


def _run_forecast(pipeline, prices: np.ndarray) -> dict:
    """Run Chronos forecast and return trend signal."""
    import torch
    context = torch.tensor(prices).unsqueeze(0)
    forecast = pipeline.predict(context, prediction_length=10)
    samples = forecast[0]  # shape: (num_samples, 10)

    median = samples.median(dim=0).values.numpy()
    q10 = samples.quantile(0.1, dim=0).numpy()
    q90 = samples.quantile(0.9, dim=0).numpy()

    last_price = float(prices[-1])
    day5 = float(median[4])
    day10 = float(median[9])

    # Trend: compare day10 median to last price
    pct_change = (day10 - last_price) / last_price
    if pct_change > 0.01:
        trend = "up"
    elif pct_change < -0.01:
        trend = "down"
    else:
        trend = "flat"

    # Confidence: ratio of quantile spread to price (lower = tighter = more confident)
    spread = float((q90 - q10).mean())
    confidence = round(spread / last_price, 4)

    return {
        "last_price": round(last_price, 2),
        "forecast_day5": round(day5, 2),
        "forecast_day10": round(day10, 2),
        "trend": trend,
        "confidence": confidence,
    }


def forecast_universe(rows, park_symbol: str | None = None) -> list:
    """Watchlist rows worth forecasting: every screened name on a venue this account can
    trade, minus the parked cash-yield ETF. Rows flagged pending_removal STAY IN — they are
    held names, and the review path reads their trend for exit suppression.

    (Until 2026-10-07 this filtered on `PortfolioWatchlist.active`, a column that never
    existed — the job crashed every night since 2026-04-05 and never wrote a forecast.)"""
    from src.portfolio.venues import partition_tradable
    tradable, _blocked = partition_tradable(rows)
    return [r for r in tradable if not (park_symbol and r.symbol == park_symbol)]


def latest_forecast(symbol: str, max_age_days: int = 3):
    """Most recent PortfolioForecast for `symbol` no older than `max_age_days`, else None.

    The job writes forecast_date = the evening it ran (17:30 ET). Consumers used to look up
    forecast_date == today, which only matched scans later that same UTC day — the next
    day's Tokyo/Europe/US scans and the monthly review (which runs BEFORE the job on the
    same evening) never saw a row. A 3-day window spans a weekend."""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    with get_db() as db:
        return (
            db.query(PortfolioForecast)
            .filter(PortfolioForecast.symbol == symbol, PortfolioForecast.forecast_date >= cutoff)
            .order_by(PortfolioForecast.forecast_date.desc())
            .first()
        )


def job_portfolio_chronos_forecast(cfg):
    """
    Nightly Chronos forecast job — runs at 17:30 ET.
    Forecasts all watchlist stocks and writes to portfolio_forecasts table.
    """
    log.info("chronos_forecast_started")
    today = date.today().strftime("%Y-%m-%d")
    processed = 0
    failed = 0

    try:
        pipeline = _get_pipeline()
    except Exception as e:
        log.error("chronos_forecast_aborted", error=str(e))
        return

    try:
        from src.portfolio.connection import get_portfolio_ib
        ib = get_portfolio_ib()
    except Exception as e:
        log.error("chronos_forecast_no_connection", error=str(e))
        return

    park_symbol = getattr(cfg, "cash_yield_symbol", None) if cfg is not None else None
    with get_db() as db:
        rows = db.query(PortfolioWatchlist).all()
        symbols = [(w.symbol, w.exchange, w.currency) for w in forecast_universe(rows, park_symbol)]

    log.info("chronos_forecast_universe", count=len(symbols))

    for symbol, exchange, currency in symbols:
        try:
            prices = _fetch_prices(ib, symbol, exchange, currency)
            if prices is None:
                failed += 1
                continue

            result = _run_forecast(pipeline, prices)

            with get_db() as db:
                # Upsert — one row per symbol per day
                existing = db.query(PortfolioForecast).filter(
                    PortfolioForecast.symbol == symbol,
                    PortfolioForecast.forecast_date == today,
                ).first()
                if existing:
                    existing.last_price = result["last_price"]
                    existing.forecast_day5 = result["forecast_day5"]
                    existing.forecast_day10 = result["forecast_day10"]
                    existing.trend = result["trend"]
                    existing.confidence = result["confidence"]
                else:
                    db.add(PortfolioForecast(
                        symbol=symbol,
                        forecast_date=today,
                        **result,
                    ))

            log.info("chronos_forecast_done", symbol=symbol,
                     trend=result["trend"], day10=result["forecast_day10"],
                     confidence=result["confidence"])
            processed += 1

        except Exception as e:
            log.warning("chronos_forecast_symbol_failed", symbol=symbol, error=str(e))
            failed += 1

    log.info("chronos_forecast_completed", processed=processed, failed=failed)
