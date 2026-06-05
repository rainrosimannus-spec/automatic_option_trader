"""
Portfolio-specific configuration — three-tier long-term portfolio.

Tiers:
  - dividend:     15% — established dividend growers, best 15yr total return
  - breakthrough: 25% — frontier tech, may not be profitable yet
  - growth:       60% — proven track record, expanding TAM
"""
from __future__ import annotations

from pydantic import BaseModel


class TierAllocation(BaseModel):
    """Configurable tier proportions. Must sum to 1.0."""
    dividend: float = 0.15
    breakthrough: float = 0.25
    growth: float = 0.60


class PutEntryConfig(BaseModel):
    """Configuration for put-selling as entry mechanism."""
    enabled: bool = True
    # If current price is within X% of target, buy directly instead
    direct_buy_threshold_pct: float = 2.0
    # Put strike = target buy price (% below current price)
    target_discount_pct: float = 5.0
    # DTE range for entry puts (longer than options trader — we want assignment)
    min_dte: int = 14
    max_dte: int = 45
    # Max contracts per stock for entry
    max_contracts: int = 1
    # If a put expires worthless, re-sell at same or lower strike
    auto_resell: bool = True


class CompounderConfig(BaseModel):
    """Knobs for the long-horizon 'compounder accumulation' strategy."""
    # Ranking: 10x fundamental score blended with 12-1 momentum percentile
    rank_fund_weight: float = 0.70
    rank_mom_weight: float = 0.30
    # Target weights
    per_name_cap_pct: float = 0.06     # cap a normal name at 6% of the portfolio
    cash_buffer_pct: float = 0.03      # keep ~3% uninvested operational cash
    # Conviction: the top fraction of the ranked universe are "leaders" — they get a higher
    # per-name cap AND are always bought directly (never routed to put-selling), so the engine
    # never under-accumulates or caps the upside of the names most likely to deliver the 10x.
    leader_top_frac: float = 0.20      # top 20% of the ranked universe = leaders
    leader_cap_pct: float = 0.10       # leaders may start at up to 10% of the portfolio
    # Tier budgets for the compounder (kept separate from the global TierAllocation so the
    # universe screener / classic strategy are untouched). Trimmed dividend → growth/breakthrough
    # to lean into the 10x engine; must sum to 1.0.
    tier_growth: float = 0.65
    tier_breakthrough: float = 0.30
    tier_dividend: float = 0.05
    # Base + crash reserve deployment — deploy the bulk within ~1 trading month, keep a small
    # crash hedge that bleeds in fast if no drawdown materializes.
    base_pct: float = 0.80             # deploy 80% as the fast-DCA'd base
    dca_horizon_days: int = 21         # base fully deployed over ≈1 month of trading days
    drawdown_tranches: list[float] = [0.10, 0.20, 0.30]  # 20% reserve fires at these SPY drawdowns
    backstop_start_days: int = 90      # if no crash within ~3mo, start bleeding the reserve in
    backstop_bleed_days: int = 180     # ...fully deployed over the following ~6mo (never idle long)
    # Entry-mode intensity thresholds
    direct_threshold: float = 0.0      # attractiveness >= this -> direct buy, else put-sell
    urgent_underweight: float = 0.5    # if >=50% below target, buy directly regardless of price
    put_target_discount_pct: float = 5.0  # CSP strike ~ this far below price when waiting
    # Per-trade sizing
    max_single_buy: float = 100000.0
    min_single_buy: float = 5000.0


class PortfolioConfig(BaseModel):
    enabled: bool = True

    # Stock-formation strategy: "compounder" (10x accumulation) or "classic" (legacy dip-buyer)
    strategy: str = "compounder"
    compounder: CompounderConfig = CompounderConfig()

    # Safety mode: suggest trades instead of placing orders
    # When true, all trades go to Approve/Reject queue on dashboard
    # When false, trades execute automatically (only for dedicated account)
    suggestion_mode: bool = True
    readonly: bool = True  # connect to IBKR in read-only mode

    # IBKR connection — separate account from options trader
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7496
    ibkr_client_id: int = 99
    ibkr_account: str = ""

    # Tier allocation (must sum to 1.0)
    tier_allocation: TierAllocation = TierAllocation()

    # Target stock counts per tier
    tier_count_dividend: int = 15
    tier_count_breakthrough: int = 25
    tier_count_growth: int = 60

    # Put-entry mechanism
    put_entry: PutEntryConfig = PutEntryConfig()

    # Buy criteria
    max_single_buy_eur: float = 100000.0
    min_single_buy_eur: float = 5000.0
    # $5M+ adaptive scaling safeguards — apply to ALL capital deployment (buys + puts + calls)
    # Per-position cap: min(NLV × 5%, $200K) — prevents single stock from dominating
    position_cap_pct: float = 0.05           # 5% of NLV per position
    position_cap_max_usd: float = 200000.0   # hard ceiling regardless of NLV
    # Total exposure cap: min(NLV × 25%, $3M) — portfolio-level ceiling
    total_exposure_pct: float = 0.25         # 25% of NLV total deployed
    total_exposure_max_usd: float = 3000000.0 # hard ceiling
    # Daily deployment limit: min(NLV × 10%, $1M) — no single-day blowout
    daily_deployment_pct: float = 0.10       # 10% of NLV per day
    daily_deployment_max_usd: float = 1000000.0 # hard ceiling
    min_discount_pct: float = 5.0
    sma_period: int = 200
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    max_portfolio_pct: float = 0.10
    cash_reserve_pct: float = 0.05

    # Margin / cash override policy
    margin_max_pct: float = 0.15              # max 15% of NLV in margin
    margin_enabled: bool = True               # allow margin in extreme conditions
    reserve_override_min_vix: float = 35.0    # VIX to unlock cash reserve
    reserve_override_min_score: float = 60.0  # min signal score to unlock reserve

    # Tier-specific buy criteria overrides
    breakthrough_min_discount_pct: float = 3.0
    breakthrough_rsi_oversold: float = 35.0
    dividend_min_discount_pct: float = 7.0
    dividend_rsi_oversold: float = 25.0

    # Market overvaluation detection
    market_overbought_pct: float = 15.0

    # Annual rescreen — December 1st
    rescreen_month: int = 12
    rescreen_day: int = 1
    rescreen_regions: str = ""
    rescreen_top_n: int = 50
    rescreen_min_market_cap: float = 1e9  # $1B minimum market cap

    # Dividend reinvestment
    reinvest_dividends: bool = True
    min_dividend_reinvest: float = 50.0

    # Cash yield
    cash_yield_enabled: bool = True
    cash_yield_symbol: str = "SGOV"
    cash_yield_exchange: str = "SMART"
    cash_yield_currency: str = "USD"

    # IPO watchlist — tickers to monitor for public listing
    ipo_watchlist: list[str] = []
    ipo_settling_days: int = 60

    # Flex Query (for deposit sync and interest data)
    flex_token: str = ""
    flex_query_id: str = ""

    # Schedule
    check_interval_hours: int = 4
    scan_hour: int = 10
    scan_minute: int = 30
