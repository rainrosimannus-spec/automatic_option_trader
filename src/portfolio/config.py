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
    # Conviction steepness for target sizing: within a tier, weight ∝ rank_score ** conviction_power.
    # 1.0 = near-flat (effective ~100 names); >1 concentrates dollars in the top-ranked names up to
    # the per-name caps (which remain the hard ceiling, so it self-limits). We trust the ranking's
    # ordering, so lean the book a little more toward the top names it surfaces.
    conviction_power: float = 1.75
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
    base_pct: float = 0.90             # deploy 90% as the fast-DCA'd base (10% parked crash reserve)
    dca_horizon_days: int = 21         # routine top-ups deploy over ≈1 month of trading days
    # Lump defusal: the DCA horizon STRETCHES with the size of the undeployed gap, so a big new deposit
    # (gap ≈ the whole base) averages in over ~lump_horizon_days instead of ~1 month — you're never
    # fully deployed right before a crash. Small routine top-ups still deploy over dca_horizon_days.
    lump_horizon_days: int = 126       # ≈6 months — full-lump deployment horizon
    # Froth throttle: slow base deployment when SPY is extended above its 200-day trend (deploy slower
    # into euphoria, full speed once at/below trend). Never throttles the crash dump. floor > 0 so a
    # persistent melt-up never fully stalls deployment (stagnation-aversion); set floor 0 for a hard pause.
    deploy_throttle_start_pct: float = 5.0    # SPY % over 200-DMA where slowing begins
    deploy_throttle_full_pct: float = 15.0    # …where the throttle hits its floor (matches overbought guard)
    deploy_throttle_floor: float = 0.25       # min pace fraction at extreme froth (never fully stop)
    # Buy-ordering policy: PRIORITY first, timing second (timing must never invert the priority).
    #   GREEN (at/below fair) — bought by rank/underweight, but only in the last `late_session_minutes` of
    #     each name's OWN session. These pulling-back names drift DOWN intraday: measured ≈ −1.8% close-vs-open
    #     and ≈ −1.0% close-vs-actual-fill over the 29 real compounder buys, confirmed on a 5-regime OHLC
    #     backtest. So a late entry is materially cheaper than firing on the first post-open scan.
    #   YELLOW (above fair, extended) — bought ONLY once the ENTIRE green list has reached target, and then
    #     with no late gate (fills early in the day). A yellow must never take budget that a still-underweight
    #     green has a claim on: yellow is the last resort, after every good-priced name is done.
    # Crash tranches bypass both gates (urgent deploy). With the 2h buy-scan cadence a 120-min window catches
    # exactly one in-window pass per market per day. late_session_only_green=False restores all-day buying with
    # the plain green-before-yellow queue order (the pre-2026-07-15 behaviour).
    late_session_only_green: bool = True
    late_session_minutes: int = 120
    drawdown_tranches: list[float] = [0.10, 0.20, 0.30]  # 20% reserve fires at these SPY drawdowns
    backstop_start_days: int = 90      # if no crash within ~3mo, start bleeding the reserve in
    backstop_bleed_days: int = 180     # ...fully deployed over the following ~6mo (never idle long)
    # Entry-mode intensity thresholds
    direct_threshold: float = 0.0      # attractiveness >= this -> direct buy, else put-sell
    urgent_underweight: float = 0.5    # if >=50% below target, buy directly regardless of price
    put_target_discount_pct: float = 5.0  # CSP strike ~ this far below price when waiting
    # Per-trade sizing — bounds SCALE with this account's NLV (see single_buy_bounds()). The portfolio
    # account grows ~$50k → $11M+; each order is sized as a % of CURRENT NLV: min 0.1% (clamped to
    # [$3,000 HARD floor, $5k cap]) and max 2%. The $3,000 floor is a hard minimum — NO order is ever
    # placed below it (a green name whose conviction-weighted target gap is < $3,000 is skipped until
    # its target grows with NLV), so the deployment concentrates into meaningful sizes and broadens as
    # the account compounds. The minimum stops growing above ~$5M (small-target tail still deploys).
    max_single_buy: float = 100000.0      # legacy/classic-path reference (compounder uses the pcts below)
    min_single_buy: float = 5000.0        # _execute_compounder_buy fallback floor only (bounds are uncapped)
    min_single_buy_pct: float = 0.004     # min order scales at 0.4% of NLV → equals the $2k floor at $500k
    max_single_buy_pct: float = 0.02      # max order = 2%   of NLV
    min_single_buy_floor: float = 2000.0  # HARD per-order floor — flat below ~$500k, then 0.4% scales above
    # Conviction-scaled DAY limit-ladder entries. Replaces the flat 0.2% under-bid in _execute_buy.
    # Core rung bids near market for HIGH-urgency names (underweight/leader/crash) so the position
    # actually fills; LOW-urgency names bid deeper to lower cost (OK to miss). Leaders also get extra
    # additive dip-adder rungs below the core, funded from the crash reserve. DAY tif → rungs expire
    # at the close and are re-evaluated next day (no GTC/cancellation; portfolio conn can't cancel).
    # In suggestion_mode the ladder collapses to a single core-rung suggestion card.
    ladder_enabled: bool = True
    entry_base_discount_pct: float = 0.2    # legacy: bid below last when the ladder is DISABLED
    # Core-rung pricing slides with urgency from a capped marketable PREMIUM (fills now) at full
    # urgency to a deep DISCOUNT (fine to miss) at zero urgency — but NEVER bids above fair value.
    entry_marketable_premium_pct: float = 0.5   # max % ABOVE last for an urgent fill (a marketable
                                                # limit, capped — fills at the ask, won't chase higher)
    entry_max_discount_pct: float = 1.5     # core-rung bid for LOW-urgency names (deep, fine to miss)
    ladder_rungs: int = 2                   # extra dip-adder rungs below the core (0 = single order)
    ladder_step_pct: float = 1.0            # spacing between dip rungs, % of price
    ladder_rung_frac: float = 0.25          # each dip rung sized at frac x core brick
    ladder_leader_only_dips: bool = True    # only leaders get dip rungs; others get core only
    # ── Leverage posture (margin account) ────────────────────────────────────────────────
    # Cash-FIRST: normal regimes deploy genuine SETTLED cash only (TotalCashValue, not the broker's
    # AvailableFunds, which already includes margin buying power and silently levered us in calm
    # markets). Margin is used ONLY when a crash drawdown-tranche has fired, and even then it is
    # bounded by crash_margin_pct and hard-stopped by the maint-margin gate below.
    crash_margin_pct: float = 0.15          # in a fired tranche, borrow up to 15% of NLV on top of cash
    # Margin is a LAST-RESORT supplement, not the primary reserve: borrowing to catch a falling knife
    # re-introduces forced-liquidation risk (the one terminal outcome a long-horizon holder must avoid),
    # which a parked CASH reserve never carries. So the facility is gated to the DEEPEST drawdown tranche
    # (true capitulation) only — not every -10% dip. The parked bill-ETF reserve is the primary dry powder.
    margin_capitulation_only: bool = True   # crash-margin only in the deepest tranche (capitulation), not any dip
    cash_park_min: float = 5000.0           # only park idle cash into the bill ETF above this (avoid churn)
    park_reserve_days: int = 10             # keep this many days of deploy budget UN-parked as cash, so
                                            # routine buys fund from cash (no ETF sale) and the ETF is only
                                            # sold once it's aged past its entry spread (no realised loss)
    margin_hard_limit_pct: float = 40.0     # block ALL new compounder buys above this maint-margin/NLV
    margin_hard_limit_crash_pct: float = 55.0  # relaxed cap while a crash tranche is active (~15% NLV loan)
    margin_soft_floor_pct: float = 25.0     # above this maint-margin level, linearly de-rate deployment
    # ── Concentration caps on NEW target sizing (winners still run untrimmed — these gate buys only) ──
    per_name_abs_ceiling: float = 750000.0  # hard $ ceiling per name on top of the 6%/10% pct caps
    sector_cap_pct: float = 0.30            # max 30% of NLV targeted into any single sector
    # ── Burn-in deployment cap (software-trust gate, NOT market timing) ───────────────────────────
    # Hard ceiling on TOTAL committed capital (filled holdings + working orders + pending suggestions).
    # While bound, the scan deploys nothing new (parking/treasury still runs). Purpose: before trusting
    # the live FX / cash-unpark / order-placement paths with a large incoming lump, cap real exposure and
    # let the funding paths prove out on real fills first. Crash dump is NOT exempt (don't lever into a
    # half-validated path). Two ways to engage — the manual flat cap always wins when set:
    #   • burn_in_max_deployed > 0  — explicit flat ceiling you arm/lift by hand (0 = off).
    #   • burn_in_auto_arm          — SELF-arming: when cumulative deposits jump by >= burn_in_trigger_
    #     deposit (a large lump starting to land, e.g. $1M/day toward $11M), hold deployment to
    #     burn_in_floor and ramp the ceiling linearly to full over burn_in_ramp_days, then auto-disarm.
    #     The small pre-deposit account (no big deposit) is NEVER throttled. IMPORTANT: deploy/restart
    #     this BEFORE the transfers begin so the arrival registers as a jump — a lump already sitting in
    #     the account on the first scan is taken as the baseline and won't arm.
    burn_in_max_deployed: float = 0.0          # manual flat ceiling (0 = off); overrides auto-arm when > 0
    burn_in_auto_arm: bool = True              # self-arm the burn-in when a large deposit lands
    burn_in_trigger_deposit: float = 500000.0  # cumulative new-deposit jump (USD) that arms the burn-in
    burn_in_floor: float = 250000.0            # day-0 deployment ceiling while armed
    burn_in_ramp_days: int = 21                # ramp the ceiling floor→full over this many days, then disarm


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
    base_currency: str = "EUR"  # account base ccy — foreign buys auto-convert into the stock's ccy via FX

    # Foreign-currency FX funding (auto-convert base→stock ccy BEFORE a foreign buy so no margin loan
    # accrues). IBKR defines only ONE direction per cash pair (e.g. EUR.HKD, never HKD.EUR), so the
    # funding code qualifies the canonical pair and derives BUY/SELL + qty from it. Below
    # fx_idealpro_min_base (the leg's value in base ccy) the conversion is under IDEALPRO's per-order
    # minimum, so we let IBKR auto-FX the buy at settlement (negligible, self-curing) rather than fire
    # a doomed order; at/above it we place a real IDEALPRO conversion and verify the fill (fail-closed).
    fx_idealpro_min_base: float = 22000.0   # ≈ USD 25k IDEALPRO minimum, expressed in base ccy
    fx_fill_wait_secs: float = 12.0         # poll this long for the FX conversion to fill before failing
    fx_funding_max_attempts: int = 6        # expire+alert an approved buy after this many failed FX tries

    # Standing foreign-DEBIT auto-close (2026-07-06): the pre-buy funder above prevents most loans, but
    # sub-IDEALPRO-min / unpriceable / no-permission legs still fall into a foreign margin loan nothing
    # sweeps. This daily pass converts base→ccy to bring any non-base debit back to a small buffer
    # (one-directional, never ccy→base) so we don't pay FX-loan interest on a CAD/GBP/etc. balance.
    # Mirrors src.strategy.fx_treasury (options account). Double-gated: no-op unless enabled; places NO
    # orders while dry_run (burn-in — logs+alerts exactly what it WOULD convert). Acts on largest debit/pass.
    fx_treasury_enabled: bool = True             # compute + act (still respects dry_run)
    fx_treasury_dry_run: bool = False            # ARMED (parity with the options acct); True = log/alert only, no orders
    fx_debit_close_threshold_pct: float = 0.005  # act only when a ccy debit exceeds this % of NLV (skip dust)
    fx_settlement_buffer_pct: float = 0.005      # after a close, leave the ccy at a small positive cushion

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

    # Cash yield — park idle EUR in an overnight money-market ETF for ~€STR yield, sellable T+1.
    # MUST be EU-UCITS (KID-eligible): the account is EUR-base and EU-domiciled, so US ETFs like SGOV
    # are rejected by IBKR (Error 201, PRIIPs — "no KID in an approved language"). XEON = Xtrackers II
    # EUR Overnight Rate Swap UCITS ETF 1C (Xetra/IBIS, EUR, accumulating, ~flat NAV). Swap CSH2 or
    # iShares ERNE here if preferred — same plumbing, just change the symbol/exchange/currency.
    cash_yield_enabled: bool = True
    cash_yield_symbol: str = "XEON"
    cash_yield_exchange: str = "IBIS"
    cash_yield_currency: str = "EUR"
    cash_yield_annual_pct: float = 2.0      # displayed annual yield of the park ETF (~€STR / ECB
                                            # deposit rate); shown on the Portfolio parked-cash card.
                                            # Set to the current overnight rate XEON tracks.

    # IPO watchlist — tickers to monitor for public listing
    ipo_watchlist: list[str] = []
    ipo_settling_days: int = 60

    # Flex Query (for deposit sync and interest data)
    flex_token: str = ""
    flex_query_id: str = ""

    # Schedule
    check_interval_hours: int = 2   # buy-scan cadence; re-prices resting orders to the current market
    scan_hour: int = 10
    scan_minute: int = 30

    # Scan liveness: alert if no portfolio buy-scan has COMPLETED within this many hours. The scan job
    # already catches its own exceptions (logs portfolio_scan_job_error) so a crash won't take down the
    # scheduler — but nothing noticed if it silently stopped deploying. The health check (every 5 min)
    # compares now() against the last successful scan and pages once it goes stale. Set comfortably above
    # check_interval_hours so a normal idle gap (off-hours) doesn't false-alarm.
    scan_staleness_alert_hours: float = 6.0
