"""
Configuration loader — merges settings.yaml + .env + env vars.
"""
from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field


_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


# ── Sub-models ──────────────────────────────────────────────
class IBKRConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 12
    timeout: int = 30
    readonly: bool = False
    account: str = ""


class DteTierLow(BaseModel):
    vix_max: float = 20
    dte_min_usd: int = 0
    dte_max_usd: int = 3
    dte_min_other: int = 0
    dte_max_other: int = 7

class DteTierMid(BaseModel):
    vix_max: float = 30
    dte_min_usd: int = 7
    dte_max_usd: int = 14
    dte_min_other: int = 7
    dte_max_other: int = 14

class DteTiers(BaseModel):
    low_vix: DteTierLow = DteTierLow()
    mid_vix: DteTierMid = DteTierMid()
    high_vix: str = "halt"

class StrategyConfig(BaseModel):
    dte_tiers: DteTiers = DteTiers()
    delta_min: float = 0.20
    delta_max: float = 0.30
    contracts_per_stock: int = 1
    min_premium: float = 0.20          # call minimum ($0.20 for covered calls)
    min_premium_put: float = 0.50       # put minimum ($0.50 for puts)
    min_open_interest: int = 10
    min_bid: float = 0.05
    min_net_premium_multiplier: float = 5.0
    min_stock_price: dict = {
        "USD": 5.0, "AUD": 2.0, "GBP": 2.0, "EUR": 2.0,
        "CHF": 5.0, "JPY": 500.0, "NOK": 20.0, "DKK": 15.0, "CAD": 2.0,
    }
    # IV Rank
    iv_rank_enabled: bool = True
    iv_rank_min: int = 30
    iv_lookback_days: int = 252
    # Dynamic delta
    dynamic_delta_enabled: bool = True
    delta_vix_low: float = 0.25
    delta_vix_low_max: float = 0.30
    delta_vix_mid: float = 0.18
    delta_vix_mid_max: float = 0.22
    delta_vix_high: float = 0.12
    delta_vix_high_max: float = 0.18
    # Profit taking / rolling
    profit_take_enabled: bool = True
    profit_take_pct: float = 0.55
    roll_enabled: bool = True
    # Earnings avoidance
    earnings_avoid_enabled: bool = True
    earnings_avoid_days: int = 3
    # Wheel
    wheel_enabled: bool = True
    cc_dte_min: int = 1
    cc_dte_max: int = 7
    cc_delta_min: float = 0.35
    cc_delta_max: float = 0.45
    cc_above_cost_basis: bool = True
    cc_progressive_strikes: bool = True
    # Hedge
    hedge_enabled: bool = True
    hedge_otm_pct: float = 0.05
    hedge_dte_target: int = 30
    hedge_roll_dte: int = 7
    hedge_budget_pct: float = 0.04
    hedge_contracts: int = 1


class RiskConfig(BaseModel):
    vix_pause_threshold: float = 30.0
    max_portfolio_positions: int = 50
    max_daily_positions: int = 10          # base daily limit (for first 100K)
    max_daily_positions_cap: int = 25      # hard cap regardless of portfolio size
    daily_position_step: float = 100000.0  # +1 trade per this amount above 100K
    max_sector_pct: float = 0.30
    max_single_stock_pct: float = 0.05
    max_buying_power_usage: float = 0.60
    max_margin_usage: float = 0.80        # block new trades when margin > 80% of NLV
    min_cash_reserve: float = 10000.0
    # Scaling safeguards ($5M+)
    position_dollar_pct: float = 0.01        # per-position cap as % of NLV
    max_position_dollars: float = 150000.0   # hard ceiling per position
    min_position_dollars: float = 25000.0    # floor — small accounts unaffected below this
    total_exposure_pct: float = 0.20         # total open collateral cap as % of NLV
    max_total_exposure: float = 2000000.0    # hard ceiling total open collateral
    daily_deployment_pct: float = 0.03       # max new collateral per day as % of NLV
    max_daily_deployment: float = 500000.0   # hard ceiling new collateral per day
    intraday_loss_halt_pct: float = 0.02     # halt if unrealized loss > 2% of NLV
    # Correlation gate (skip if NLV < 50K or fewer than 3 open positions)
    max_correlation: float = 0.85        # block if avg pairwise correlation > this
    correlation_nlv_threshold: float = 50000.0
    correlation_lookback_days: int = 60
    # Delta exposure gate (skip if NLV < 50K)
    max_portfolio_delta: float = 500.0   # total abs delta units across all open puts
    delta_nlv_threshold: float = 50000.0
    # SPY MA gate
    spy_ma_enabled: bool = True
    spy_ma_fast: int = 10
    spy_ma_slow: int = 20
    spy_bearish_reduction: float = 0.50


class ScheduleConfig(BaseModel):
    market_open: str = "09:30"
    market_close: str = "16:00"
    timezone: str = "US/Eastern"
    scan_interval_minutes: int = 30
    position_check_minutes: int = 5
    enabled_markets: list[str] = []  # empty = all markets; set ["SMART"] to limit to US only


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False


class AppConfig(BaseModel):
    name: str = "Options Trader"
    mode: Literal["paper", "live"] = "paper"
    suggestion_mode: bool = False    # true = suggest trades with Approve/Reject
    log_level: str = "INFO"
    db_path: str = "data/trades.db"


# ── Portfolio config ─────────────────────────────────────────
from src.portfolio.config import PortfolioConfig


# ── Root config ─────────────────────────────────────────────
class Settings(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    app: AppConfig = AppConfig()
    ibkr: IBKRConfig = IBKRConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    web: WebConfig = WebConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    raw: dict = {}  # full YAML dict for sections not modeled (alerts, bridge)


def _load_yaml(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _apply_env_overrides(settings: Settings) -> Settings:
    """Override selected settings from environment variables."""
    env = os.environ
    if v := env.get("IBKR_HOST"):
        settings.ibkr.host = v
    if v := env.get("IBKR_PORT"):
        settings.ibkr.port = int(v)
    if v := env.get("IBKR_CLIENT_ID"):
        settings.ibkr.client_id = int(v)
    if v := env.get("IBKR_ACCOUNT"):
        settings.ibkr.account = v
    if v := env.get("TRADING_MODE"):
        settings.app.mode = v  # type: ignore[assignment]
    if v := env.get("WEB_HOST"):
        settings.web.host = v
    if v := env.get("WEB_PORT"):
        settings.web.port = int(v)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache application settings."""
    # Load .env if present
    env_file = _CONFIG_DIR / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)

    raw = _load_yaml(_CONFIG_DIR / "settings.yaml")
    settings = Settings(**raw, raw=raw)
    settings = _apply_env_overrides(settings)
    return settings


# ── Watchlist loader ────────────────────────────────────────
class StockEntry(BaseModel):
    symbol: str
    name: str
    sector: str
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str | None = None
    options_exchange: str | None = None    # derivatives exchange (DTB, ICEEU, OSE, etc.)
    contract_size: int = 100          # options multiplier (100 for US/EU, varies for Japan)
    div_yield: float | None = None
    category: Literal["growth", "dividend"] = "growth"

    @property
    def opt_exchange(self) -> str:
        """Return the options exchange — falls back to stock exchange if not set."""
        return self.options_exchange or self.exchange


@lru_cache(maxsize=1)
def get_watchlist() -> list[StockEntry]:
    """Load the portfolio watchlist (watchlist.yaml)."""
    raw = _load_yaml(_CONFIG_DIR / "watchlist.yaml")
    stocks: list[StockEntry] = []
    for item in raw.get("growth", []):
        item["symbol"] = str(item["symbol"])
        stocks.append(StockEntry(**item, category="growth"))
    for item in raw.get("dividend", []):
        item["symbol"] = str(item["symbol"])
        stocks.append(StockEntry(**item, category="dividend"))
    return stocks


@lru_cache(maxsize=1)
def get_options_universe() -> list[StockEntry]:
    """
    Load the options trading universe (options_universe.yaml).
    This is the top 50 stocks ranked by options_score (fundamentals + liquidity).
    Generated monthly by the screener job running on portfolio port 7496.
    Falls back to watchlist.yaml if options_universe.yaml does not exist yet.
    """
    options_path = _CONFIG_DIR / "options_universe.yaml"
    if not options_path.exists():
        import logging
        logging.getLogger(__name__).warning(
            "options_universe.yaml not found — falling back to watchlist.yaml. "
            "Run the monthly screener to generate it."
        )
        return get_watchlist()

    raw = _load_yaml(options_path)
    stocks: list[StockEntry] = []
    for item in raw.get("stocks", []):
        item["symbol"] = str(item["symbol"])
        # tier field in options_universe maps to category for StockEntry
        tier = item.pop("tier", "growth")
        category = "dividend" if tier == "dividend" else "growth"
        # Remove options-specific fields not in StockEntry
        for key in ["options_score", "options_liquidity", "rank"]:
            item.pop(key, None)
        try:
            stocks.append(StockEntry(**item, category=category))
        except Exception:
            pass
    return stocks
