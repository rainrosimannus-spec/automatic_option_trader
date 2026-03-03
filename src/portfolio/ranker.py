"""
Portfolio Ranker — portfolio-aware signal ranking and cash/margin policy.

Two responsibilities:
  1. Rank buy signals considering both signal quality AND portfolio context
  2. Determine deployable capital: normal cash, reserve override, or margin

Ranking factors:
  - Signal score (from analyzer): 60% weight — how good is this entry point?
  - Tier underweight bonus: 20% weight — fill underweight tiers first
  - Concentration penalty: 10% weight — reduce if already >5% of portfolio
  - Sector diversity bonus: 10% weight — prefer sectors we lack

Cash / Margin tiers:
  - Normal: deploy cash above 5% reserve (default)
  - Reserve override: deploy into 5% reserve (VIX>35 + strong signal)
  - Margin tier 1: borrow up to 7.5% NLV (capitulation: VIX>45, SPY crash)
  - Margin tier 2: borrow up to 15% NLV (stabilization: VIX declining, floor forming)
  - Tier-specific limits: dividend stocks get full margin, breakthrough gets half
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.core.logger import get_logger

log = get_logger(__name__)


# ── Market regime detection ────────────────────────────────────
@dataclass
class MarketRegime:
    """Current market conditions for cash/margin decisions."""
    vix: float = 17.0
    vix_10d_ago: float | None = None          # for detecting VIX declining
    spy_price: float = 0.0
    spy_200d_sma: float = 0.0
    spy_10d_sma: float = 0.0
    spy_52w_high: float = 0.0
    spy_pct_below_200sma: float = 0.0         # positive = below SMA
    spy_pct_below_52w_high: float = 0.0       # drawdown from high

    @property
    def is_capitulation(self) -> bool:
        """
        Blood in the streets: extreme fear + major crash.
        VIX > 45 AND SPY > 20% below 52w high.
        Historical: March 2020, Oct 2008, Aug 2015.
        """
        return self.vix > 45 and self.spy_pct_below_52w_high > 20

    @property
    def is_stabilization(self) -> bool:
        """
        Market found a floor and is rebounding from a crash.
        Conditions:
          - VIX was elevated (>35) but is now declining (below 30)
          - SPY is above its 10d SMA (short-term uptrend)
          - SPY is still >15% below 200d SMA (still deeply discounted)
        This catches the "new normal" rebound phase.
        """
        vix_declining = (
            self.vix_10d_ago is not None
            and self.vix_10d_ago > 35
            and self.vix < 30
        )
        spy_rebounding = (
            self.spy_10d_sma > 0
            and self.spy_price > self.spy_10d_sma
        )
        spy_still_cheap = self.spy_pct_below_200sma > 15

        return vix_declining and spy_rebounding and spy_still_cheap

    @property
    def is_elevated_fear(self) -> bool:
        """VIX > 35 — real fear, not just a bad day."""
        return self.vix > 35

    @property
    def regime_name(self) -> str:
        if self.is_capitulation:
            return "capitulation"
        elif self.is_stabilization:
            return "stabilization"
        elif self.is_elevated_fear:
            return "elevated_fear"
        else:
            return "normal"


# ── Cash / Margin policy ──────────────────────────────────────
@dataclass
class CashPolicy:
    """Determines how much capital to deploy based on market regime."""
    # Normal operation
    cash_reserve_pct: float = 0.05            # keep 5% cash normally
    # Override: dip into reserve
    reserve_override_min_vix: float = 35.0    # VIX threshold to unlock reserve
    reserve_override_min_score: float = 60.0  # minimum composite score to unlock
    # Margin tier 1: capitulation
    margin_max_pct: float = 0.15              # max 15% of NLV in margin
    capitulation_margin_pct: float = 0.075    # use up to 7.5% in capitulation
    # Margin tier 2: stabilization (can use full 15%)
    stabilization_margin_pct: float = 0.15    # full margin in stabilization
    # Tier-specific margin multipliers (1.0 = full margin allowed)
    tier_margin_multiplier: dict = field(default_factory=lambda: {
        "dividend": 1.0,       # full margin — safest, income-producing
        "growth": 0.75,        # 75% of margin limit — proven but volatile
        "breakthrough": 0.5,   # 50% of margin limit — speculative
    })

    def get_deployable(
        self,
        available_cash: float,
        net_liquidation: float,
        regime: MarketRegime,
        best_signal_score: float = 0.0,
    ) -> tuple[float, str]:
        """
        Calculate total deployable capital and funding source.
        Returns (deployable_amount, source: "cash" | "reserve" | "margin").
        """
        reserve = net_liquidation * self.cash_reserve_pct
        normal_deployable = max(0, available_cash - reserve)

        # Tier 1: Normal — just cash above reserve
        if regime.regime_name == "normal" and not regime.is_elevated_fear:
            return normal_deployable, "cash"

        # Tier 2: Elevated fear + strong signal → dip into reserve
        if regime.is_elevated_fear and best_signal_score >= self.reserve_override_min_score:
            # Can use ALL cash (reserve = 0)
            reserve_extra = min(reserve, available_cash - normal_deployable)
            deployable = available_cash  # use everything
            log.info("cash_reserve_override",
                     vix=regime.vix,
                     signal_score=best_signal_score,
                     extra_from_reserve=round(reserve_extra, 2))
            return deployable, "reserve"

        # Tier 3: Capitulation → use margin (up to 7.5%)
        if regime.is_capitulation:
            margin_available = net_liquidation * self.capitulation_margin_pct
            deployable = available_cash + margin_available
            log.info("margin_capitulation_mode",
                     vix=regime.vix,
                     spy_drawdown=round(regime.spy_pct_below_52w_high, 1),
                     margin_available=round(margin_available, 2),
                     total_deployable=round(deployable, 2))
            return deployable, "margin_capitulation"

        # Tier 4: Stabilization → use full margin (up to 15%)
        if regime.is_stabilization:
            margin_available = net_liquidation * self.stabilization_margin_pct
            deployable = available_cash + margin_available
            log.info("margin_stabilization_mode",
                     vix=regime.vix,
                     spy_above_10d=True,
                     spy_pct_below_200sma=round(regime.spy_pct_below_200sma, 1),
                     margin_available=round(margin_available, 2),
                     total_deployable=round(deployable, 2))
            return deployable, "margin_stabilization"

        # Elevated fear but signal not strong enough → still override reserve
        if regime.is_elevated_fear:
            return available_cash, "reserve"

        return normal_deployable, "cash"

    def get_tier_margin_limit(self, tier: str, net_liquidation: float,
                              regime: MarketRegime) -> float:
        """Max margin for a single stock based on its tier."""
        multiplier = self.tier_margin_multiplier.get(tier, 0.5)
        if regime.is_capitulation:
            base_margin = net_liquidation * self.capitulation_margin_pct
        elif regime.is_stabilization:
            base_margin = net_liquidation * self.stabilization_margin_pct
        else:
            return 0.0  # no margin in normal times
        # Per-stock limit: tier multiplier * base, but max 5% NLV per stock
        per_stock_max = net_liquidation * 0.05
        return min(base_margin * multiplier, per_stock_max)


# ── Portfolio-aware ranking ───────────────────────────────────
@dataclass
class RankedSignal:
    """A buy signal enriched with portfolio-aware ranking."""
    symbol: str
    tier: str
    sector: str
    # Raw signal
    composite_score: float       # from analyzer (0-100)
    signal_type: str
    discount_pct: float
    rsi: float | None
    # Portfolio adjustments
    tier_underweight_bonus: float = 0.0    # 0-20 pts
    concentration_penalty: float = 0.0      # 0-10 pts (subtracted)
    sector_diversity_bonus: float = 0.0     # 0-10 pts
    # Final
    final_rank_score: float = 0.0
    rank: int = 0
    funding_source: str = "cash"           # cash, reserve, margin

    @property
    def is_margin_trade(self) -> bool:
        return "margin" in self.funding_source


def rank_signals(
    signals: list[tuple],  # list of (PortfolioWatchlist, StockAnalysis)
    holdings: dict[str, float],  # symbol → current market value
    tier_weights: dict[str, float],  # tier → target allocation (0-1)
    tier_values: dict[str, float],  # tier → current total value
    sector_counts: dict[str, int],  # sector → count of existing holdings
    net_liquidation: float,
    regime: MarketRegime,
    cash_policy: CashPolicy,
) -> list[RankedSignal]:
    """
    Rank buy signals with portfolio-aware scoring.

    Final score = (signal_score × 0.60)
                + (tier_underweight_bonus × 0.20)
                + (sector_diversity_bonus × 0.10)
                - (concentration_penalty × 0.10)

    Returns sorted list, highest rank first.
    """
    if not signals:
        return []

    total_value = sum(tier_values.values()) or net_liquidation or 1.0
    max_sector_count = max(sector_counts.values()) if sector_counts else 1

    ranked = []

    for stock, analysis in signals:
        rs = RankedSignal(
            symbol=stock.symbol,
            tier=stock.tier,
            sector=getattr(stock, 'sector', ''),
            composite_score=analysis.composite_score,
            signal_type=analysis.signal_type,
            discount_pct=analysis.discount_pct or 0,
            rsi=analysis.rsi_14,
        )

        # ── Tier underweight bonus (0-20 pts) ──
        # If a tier is below its target allocation, stocks in that tier get a bonus
        target_pct = tier_weights.get(stock.tier, 0.33)
        current_pct = tier_values.get(stock.tier, 0) / total_value if total_value > 0 else 0
        underweight_pct = max(0, target_pct - current_pct)
        # Scale: 0% underweight → 0 pts, 10%+ underweight → 20 pts
        rs.tier_underweight_bonus = min(20, underweight_pct * 200)

        # ── Concentration penalty (0-10 pts) ──
        # If we already have a large position, reduce attractiveness
        stock_value = holdings.get(stock.symbol, 0)
        stock_pct = stock_value / total_value if total_value > 0 else 0
        if stock_pct > 0.05:
            # 5% → 0 penalty, 10% → 10 penalty (max)
            rs.concentration_penalty = min(10, (stock_pct - 0.05) * 200)
        elif stock_pct > 0.03:
            # Mild penalty 3-5%
            rs.concentration_penalty = (stock_pct - 0.03) * 100

        # ── Sector diversity bonus (0-10 pts) ──
        # Prefer sectors we have fewer holdings in
        sector = getattr(stock, 'sector', '')
        sector_count = sector_counts.get(sector, 0)
        if max_sector_count > 0 and sector:
            # Fewer holdings in sector → higher bonus
            diversity_ratio = 1.0 - (sector_count / (max_sector_count + 1))
            rs.sector_diversity_bonus = diversity_ratio * 10

        # ── Compute final rank score ──
        rs.final_rank_score = (
            rs.composite_score * 0.60
            + rs.tier_underweight_bonus * 0.20
            + rs.sector_diversity_bonus * 0.10
            - rs.concentration_penalty * 0.10
        )

        # ── Determine funding source ──
        if regime.is_capitulation or regime.is_stabilization:
            tier_limit = cash_policy.get_tier_margin_limit(
                stock.tier, net_liquidation, regime
            )
            if tier_limit > 0:
                rs.funding_source = f"margin_{regime.regime_name}"
            else:
                rs.funding_source = "cash"
        elif regime.is_elevated_fear and rs.composite_score >= cash_policy.reserve_override_min_score:
            rs.funding_source = "reserve"
        else:
            rs.funding_source = "cash"

        ranked.append(rs)

    # Sort by final rank score descending
    ranked.sort(key=lambda r: r.final_rank_score, reverse=True)

    # Assign ranks
    for i, rs in enumerate(ranked):
        rs.rank = i + 1

    if ranked:
        log.info("portfolio_ranking_complete",
                 count=len(ranked),
                 regime=regime.regime_name,
                 top3=[(r.symbol, round(r.final_rank_score, 1), r.funding_source)
                       for r in ranked[:3]])

    return ranked


def detect_market_regime(
    vix: float | None,
    spy_data: dict | None,
) -> MarketRegime:
    """
    Build MarketRegime from available market data.

    spy_data should contain:
      price, sma_200, sma_10, high_52w, vix_10d_ago (optional)
    """
    regime = MarketRegime()

    if vix is not None:
        regime.vix = vix

    if spy_data:
        regime.spy_price = spy_data.get("price", 0)
        regime.spy_200d_sma = spy_data.get("sma_200", 0)
        regime.spy_10d_sma = spy_data.get("sma_10", 0)
        regime.spy_52w_high = spy_data.get("high_52w", 0)
        regime.vix_10d_ago = spy_data.get("vix_10d_ago")

        if regime.spy_200d_sma > 0:
            regime.spy_pct_below_200sma = max(0, (
                (regime.spy_200d_sma - regime.spy_price) / regime.spy_200d_sma * 100
            ))

        if regime.spy_52w_high > 0:
            regime.spy_pct_below_52w_high = max(0, (
                (regime.spy_52w_high - regime.spy_price) / regime.spy_52w_high * 100
            ))

    log.info("market_regime_detected",
             regime=regime.regime_name,
             vix=round(regime.vix, 1),
             spy_below_200sma=round(regime.spy_pct_below_200sma, 1),
             spy_below_52w_high=round(regime.spy_pct_below_52w_high, 1))

    return regime
