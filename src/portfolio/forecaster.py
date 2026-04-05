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
        contract = Stock(symbol, exchange, currency)
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

    with get_db() as db:
        watchlist = db.query(PortfolioWatchlist).filter(
            PortfolioWatchlist.active == True
        ).all()
        symbols = [(w.symbol, w.exchange, w.currency) for w in watchlist]

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
