"""
Consigliere Analyzer — the independent advisor brain.

SCOPE: Macro-level system improvements ONLY.
  - Rule changes (delta range, DTE targets, IV thresholds)
  - Limit changes (position size, sector caps, daily trade limits)
  - Allocation drift (tier balance, sector rotation)
  - Risk policy (margin limits, VIX thresholds, hedge sizing)
  - Process observations (acceptance rate, expiry patterns)
  - Leverage/margin health

NEVER suggests specific trades, symbols, or buy/sell actions.
That's the portfolio buyer's and options scanner's job.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.core.database import get_db
from src.core.logger import get_logger
from src.consigliere.models import ConsigliereMemo

log = get_logger(__name__)


class Consigliere:
    """The independent advisor — macro-level rule and limit suggestions only."""

    def run_daily_review(self):
        """
        Main review cycle — runs daily after market close.
        Checks system health, rule effectiveness, and suggests improvements.
        """
        log.info("consigliere_review_started")

        findings = []
        modules = [
            ("options_effectiveness", self._review_options_effectiveness),
            ("portfolio_allocation", self._review_portfolio_allocation),
            ("process_health", self._review_process_health),
            ("risk_policy", self._review_risk_policy),
            ("leverage_health", self._review_leverage_health),
            ("parameter_calibration", self._review_parameter_calibration),
        ]

        for name, fn in modules:
            try:
                findings += fn()
            except Exception as e:
                log.warning(f"consigliere_{name}_error", error=str(e))

        if findings:
            with get_db() as db:
                for memo in findings:
                    db.add(memo)

            log.info("consigliere_review_done", memos=len(findings))
        else:
            log.info("consigliere_review_done", memos=0)

        return findings

    # ══════════════════════════════════════════════════════════
    # MODULE 1: OPTIONS STRATEGY EFFECTIVENESS
    # Suggests rule changes to delta, DTE, IV thresholds
    # ══════════════════════════════════════════════════════════

    def _review_options_effectiveness(self) -> list[ConsigliereMemo]:
        from src.core.models import Position, PositionStatus

        memos = []

        with get_db() as db:
            closed = db.query(Position).filter(
                Position.status.in_([PositionStatus.CLOSED, PositionStatus.EXPIRED])
            ).all()

        if len(closed) < 10:
            return memos

        winners = [p for p in closed if p.realized_pnl >= 0]
        win_rate = len(winners) / len(closed) * 100

        # Win rate too low → suggest tightening delta
        if win_rate < 70:
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="suggestion",
                title=f"Win rate {win_rate:.0f}% — consider tightening delta range",
                body=(
                    f"Across {len(closed)} closed positions, win rate is {win_rate:.1f}%. "
                    f"Target is 80%+. Recommendation: reduce delta_max by 0.02-0.03 "
                    f"in settings.yaml to filter for higher-probability trades. "
                    f"Also consider increasing min_premium to skip low-reward setups."
                ),
                metric_name="win_rate",
                metric_value=win_rate,
                metric_benchmark=80.0,
            ))

        # Win rate very high → suggest widening delta for more income
        if win_rate > 92 and len(closed) >= 20:
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="info",
                title=f"Win rate {win_rate:.0f}% — room to widen delta for more income",
                body=(
                    f"Win rate of {win_rate:.1f}% across {len(closed)} positions suggests "
                    f"the delta range may be too conservative. Consider raising delta_max "
                    f"by 0.02 to capture more premium without materially increasing risk."
                ),
                metric_name="win_rate",
                metric_value=win_rate,
                metric_benchmark=80.0,
            ))

        # Assignment rate → suggest delta adjustment
        assigned = [p for p in closed if p.position_type == "stock"]
        if closed:
            assignment_rate = len(assigned) / len(closed) * 100
            if assignment_rate > 30:
                memos.append(ConsigliereMemo(
                    category="improvement",
                    severity="suggestion",
                    title=f"Assignment rate {assignment_rate:.0f}% — reduce delta_max",
                    body=(
                        f"{len(assigned)} of {len(closed)} positions resulted in assignment. "
                        f"Above 25% ties up too much capital in stock. "
                        f"Suggestion: lower delta_max by 0.03 or increase min_dte "
                        f"to give more time for recovery before expiry."
                    ),
                    metric_name="assignment_rate",
                    metric_value=assignment_rate,
                    metric_benchmark=20.0,
                ))

        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 2: PORTFOLIO ALLOCATION DRIFT
    # Flags tier imbalance, sector concentration
    # ══════════════════════════════════════════════════════════

    def _review_portfolio_allocation(self) -> list[ConsigliereMemo]:
        from src.portfolio.models import PortfolioHolding

        memos = []

        with get_db() as db:
            holdings = db.query(PortfolioHolding).filter(
                PortfolioHolding.shares > 0
            ).all()

        if not holdings:
            return memos

        total_value = sum(h.market_value or h.total_invested or 0 for h in holdings)
        if total_value <= 0:
            return memos

        # Tier allocation vs targets
        tier_targets = {"dividend": 0.25, "growth": 0.50, "breakthrough": 0.25}
        tier_values: dict[str, float] = {}
        for h in holdings:
            tier = h.tier or "growth"
            tier_values[tier] = tier_values.get(tier, 0) + (h.market_value or 0)

        drifted_tiers = []
        for tier, target in tier_targets.items():
            actual = tier_values.get(tier, 0) / total_value
            drift = actual - target
            if abs(drift) > 0.10:
                drifted_tiers.append((tier, actual, target, drift))

        if drifted_tiers:
            details = ", ".join(
                f"{t[0]} {t[1]:.0%} (target {t[2]:.0%})" for t in drifted_tiers
            )
            memos.append(ConsigliereMemo(
                category="portfolio",
                severity="suggestion",
                title="Tier allocation drift >10%",
                body=(
                    f"Portfolio tier allocation has drifted: {details}. "
                    f"Consider adjusting the rescreen scoring weights or "
                    f"tier_allocation percentages in portfolio config to "
                    f"rebalance over time. No immediate action needed — "
                    f"the buyer will naturally favor underweight tiers."
                ),
                metric_name="tier_drift_count",
                metric_value=float(len(drifted_tiers)),
                metric_benchmark=0.0,
            ))

        # Sector concentration
        sector_values: dict[str, float] = {}
        for h in holdings:
            s = h.sector or "Unknown"
            sector_values[s] = sector_values.get(s, 0) + (h.market_value or 0)

        for sector, val in sector_values.items():
            pct = val / total_value
            if pct > 0.35:
                memos.append(ConsigliereMemo(
                    category="risk",
                    severity="suggestion",
                    title=f"Sector concentration: {sector} at {pct:.0%}",
                    body=(
                        f"{sector} is {pct:.1%} of portfolio, above the 30% sector cap. "
                        f"Consider raising the sector_max_concentration parameter "
                        f"penalty in the ranker, or manually adjusting the watchlist "
                        f"to include more stocks from underrepresented sectors."
                    ),
                    metric_name="sector_concentration",
                    metric_value=pct * 100,
                    metric_benchmark=30.0,
                ))

        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 3: PROCESS HEALTH
    # Acceptance rate, expiry patterns, system usage
    # ══════════════════════════════════════════════════════════

    def _review_process_health(self) -> list[ConsigliereMemo]:
        from src.core.suggestions import TradeSuggestion

        memos = []

        with get_db() as db:
            cutoff = datetime.utcnow() - timedelta(days=30)
            suggestions = db.query(TradeSuggestion).filter(
                TradeSuggestion.created_at >= cutoff
            ).all()

        if len(suggestions) < 5:
            return memos

        approved = [s for s in suggestions if s.status == "approved"]
        rejected = [s for s in suggestions if s.status == "rejected"]
        expired = [s for s in suggestions if s.status == "expired"]
        total_reviewed = len(approved) + len(rejected)

        # Low acceptance → system generating poor signals
        if total_reviewed > 5:
            accept_rate = len(approved) / total_reviewed * 100
            if accept_rate < 25:
                memos.append(ConsigliereMemo(
                    category="improvement",
                    severity="suggestion",
                    title=f"Suggestion acceptance rate: {accept_rate:.0f}%",
                    body=(
                        f"Only {len(approved)} of {total_reviewed} suggestions approved. "
                        f"The system may be generating too many low-quality signals. "
                        f"Consider: raising IV rank minimum, narrowing the watchlist, "
                        f"increasing min_premium, or tightening delta range to improve "
                        f"signal-to-noise ratio."
                    ),
                    metric_name="acceptance_rate",
                    metric_value=accept_rate,
                    metric_benchmark=60.0,
                ))

        # High expiry rate → notifications not working or review window too short
        if suggestions:
            expire_rate = len(expired) / len(suggestions) * 100
            if expire_rate > 50:
                memos.append(ConsigliereMemo(
                    category="improvement",
                    severity="info",
                    title=f"Suggestion expiry rate: {expire_rate:.0f}%",
                    body=(
                        f"{len(expired)} of {len(suggestions)} suggestions expired unreviewed. "
                        f"Consider extending expires_hours in config, "
                        f"or check that ntfy notifications are working properly."
                    ),
                    metric_name="expiry_rate",
                    metric_value=expire_rate,
                    metric_benchmark=20.0,
                ))

        # Options vs portfolio acceptance difference
        opt_sugg = [s for s in suggestions if s.source == "options"]
        port_sugg = [s for s in suggestions if s.source == "portfolio"]
        opt_approved = len([s for s in opt_sugg if s.status == "approved"])
        port_approved = len([s for s in port_sugg if s.status == "approved"])

        if len(opt_sugg) > 10 and opt_approved == 0:
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="suggestion",
                title="All options suggestions rejected — recalibrate scanner",
                body=(
                    f"Generated {len(opt_sugg)} options suggestions in 30 days, "
                    f"none approved. The options scanner needs recalibration. "
                    f"Check: delta range, watchlist quality, min_premium, "
                    f"and IV rank threshold. The scanner may be targeting "
                    f"stocks or strikes you're not comfortable with."
                ),
            ))

        if len(port_sugg) > 10 and port_approved == 0:
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="suggestion",
                title="All portfolio suggestions rejected — review buy signals",
                body=(
                    f"Generated {len(port_sugg)} portfolio buy suggestions in 30 days, "
                    f"none approved. Review the SMA discount thresholds, RSI levels, "
                    f"and composite score weights. The analyzer may be triggering "
                    f"buy signals at levels you consider too risky."
                ),
            ))

        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 4: RISK POLICY REVIEW
    # Expiry clustering, position limits, hedge coverage
    # ══════════════════════════════════════════════════════════

    def _review_risk_policy(self) -> list[ConsigliereMemo]:
        from src.core.models import Position, PositionStatus

        memos = []

        with get_db() as db:
            open_positions = db.query(Position).filter(
                Position.status == PositionStatus.OPEN
            ).all()

        if not open_positions:
            return memos

        short_puts = [p for p in open_positions if p.position_type == "short_put"]
        stocks = [p for p in open_positions if p.position_type == "stock"]
        calls = [p for p in open_positions if p.position_type == "covered_call"]

        # Expiry clustering
        from collections import Counter
        expiry_weeks = Counter()
        for p in short_puts:
            if p.expiry:
                try:
                    exp_date = datetime.strptime(p.expiry, "%Y%m%d").date()
                    week_key = exp_date.isocalendar()[1]
                    expiry_weeks[week_key] += 1
                except Exception:
                    pass

        for week, count in expiry_weeks.items():
            if count >= 5:
                memos.append(ConsigliereMemo(
                    category="risk",
                    severity="warning",
                    title=f"{count} puts expiring same week — stagger DTE targets",
                    body=(
                        f"Week {week} has {count} short puts expiring together. "
                        f"This creates concentrated assignment risk. "
                        f"Consider adding DTE variety to the scanner config: "
                        f"alternate between 7-14 DTE and 21-35 DTE targets, "
                        f"or set max_same_week_expiries limit."
                    ),
                    metric_name="expiry_cluster",
                    metric_value=float(count),
                    metric_benchmark=3.0,
                ))

        # Assigned stock without covered calls
        if stocks:
            stock_symbols = {p.symbol for p in stocks}
            call_symbols = {p.symbol for p in calls}
            uncovered = stock_symbols - call_symbols

            if len(uncovered) > 2:
                memos.append(ConsigliereMemo(
                    category="improvement",
                    severity="suggestion",
                    title=f"{len(uncovered)} assigned stocks without covered calls",
                    body=(
                        f"Stocks assigned but no calls written: "
                        f"{', '.join(sorted(uncovered)[:5])}. "
                        f"The covered call writer may need a shorter check interval "
                        f"or the progressive strike rules may be too restrictive. "
                        f"Consider lowering the initial call strike offset."
                    ),
                    metric_name="uncovered_assignments",
                    metric_value=float(len(uncovered)),
                    metric_benchmark=1.0,
                ))

        # Daily trade limit utilization
        today_count = len([
            p for p in short_puts
            if p.opened_at and p.opened_at.date() == datetime.utcnow().date()
        ])
        # If consistently hitting daily limit, suggest raising it
        # (check last 7 days)
        with get_db() as db:
            week_ago = datetime.utcnow() - timedelta(days=7)
            from sqlalchemy import func
            daily_counts = db.query(
                func.date(Position.opened_at),
                func.count(Position.id)
            ).filter(
                Position.opened_at >= week_ago,
                Position.position_type == "short_put",
            ).group_by(func.date(Position.opened_at)).all()

        if daily_counts:
            maxed_days = len([d for d in daily_counts if d[1] >= 10])
            if maxed_days >= 3:
                memos.append(ConsigliereMemo(
                    category="improvement",
                    severity="info",
                    title=f"Hit daily trade limit {maxed_days} of {len(daily_counts)} days",
                    body=(
                        f"The 10-trade daily limit was reached on {maxed_days} days "
                        f"this week. If the system consistently finds more than 10 "
                        f"valid opportunities, consider raising max_daily_trades. "
                        f"Alternatively, tighten the screener to be more selective."
                    ),
                    metric_name="limit_hit_days",
                    metric_value=float(maxed_days),
                    metric_benchmark=1.0,
                ))

        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 5: LEVERAGE & MARGIN HEALTH
    # Account-level risk from real IBKR data
    # ══════════════════════════════════════════════════════════

    def _review_leverage_health(self) -> list[ConsigliereMemo]:
        memos = []

        try:
            from src.broker.account import get_account_summary
            acct = get_account_summary()
        except Exception:
            return memos

        nlv = acct.net_liquidation
        if nlv <= 0:
            return memos

        margin_pct = acct.maintenance_margin / nlv * 100

        if margin_pct > 50:
            severity = "critical" if margin_pct > 70 else "warning"
            memos.append(ConsigliereMemo(
                category="risk",
                severity=severity,
                title=f"Margin utilization: {margin_pct:.0f}%",
                body=(
                    f"Maintenance margin is €{acct.maintenance_margin:,.0f} against "
                    f"€{nlv:,.0f} NLV. "
                    f"{'CRITICAL: margin call territory. ' if margin_pct > 70 else ''}"
                    f"The margin gate in the portfolio buyer is blocking new "
                    f"purchases (threshold: 40%). This is working as designed. "
                    f"If this persists, consider reducing the options position "
                    f"size (contracts_per_stock) or narrowing the watchlist."
                ),
                metric_name="margin_utilization",
                metric_value=margin_pct,
                metric_benchmark=40.0,
            ))

        if acct.cash_balance < 0:
            memos.append(ConsigliereMemo(
                category="risk",
                severity="warning",
                title=f"Negative cash: €{acct.cash_balance:,.0f}",
                body=(
                    f"Account is borrowing on margin. Interest accruing. "
                    f"The buyer's margin gate will block new purchases. "
                    f"To recover: let short puts expire, reduce open "
                    f"positions, or wait for covered call assignments. "
                    f"If this becomes chronic, reduce contracts_per_stock "
                    f"or max_position_pct in settings."
                ),
                metric_name="cash_balance",
                metric_value=acct.cash_balance,
                metric_benchmark=0.0,
            ))

        # Short put exposure vs buying power
        from src.core.models import Position, PositionStatus
        with get_db() as db:
            open_puts = db.query(Position).filter(
                Position.status == PositionStatus.OPEN,
                Position.position_type == "short_put",
            ).all()

        max_assignment = sum(
            (p.strike or 0) * (p.quantity or 1) * 100
            for p in open_puts
        )

        if max_assignment > 0 and acct.buying_power > 0:
            ratio = max_assignment / acct.buying_power * 100
            if ratio > 80:
                memos.append(ConsigliereMemo(
                    category="risk",
                    severity="warning",
                    title=f"Put exposure {ratio:.0f}% of buying power",
                    body=(
                        f"Maximum assignment cost: €{max_assignment:,.0f} vs "
                        f"buying power €{acct.buying_power:,.0f}. "
                        f"If most puts get assigned simultaneously, the account "
                        f"could face a margin call. Consider reducing "
                        f"contracts_per_stock, lowering max_daily_trades, "
                        f"or increasing the position_size_pct cap."
                    ),
                    metric_name="put_exposure_ratio",
                    metric_value=ratio,
                    metric_benchmark=60.0,
                ))

        bp_pct = acct.buying_power / nlv * 100 if nlv > 0 else 0
        if 0 < bp_pct < 15:
            memos.append(ConsigliereMemo(
                category="risk",
                severity="warning",
                title=f"Buying power cushion: {bp_pct:.0f}% of NLV",
                body=(
                    f"Only €{acct.buying_power:,.0f} buying power remaining. "
                    f"A 5-10% market drop could trigger margin calls. "
                    f"Consider lowering the margin policy limits: "
                    f"reduce margin_max_pct from 15% to 10%, or "
                    f"temporarily pause the options scanner."
                ),
                metric_name="buying_power_pct",
                metric_value=bp_pct,
                metric_benchmark=25.0,
            ))

        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 6: PARAMETER CALIBRATION
    # Are the current settings optimal based on outcomes?
    # ══════════════════════════════════════════════════════════

    def _review_parameter_calibration(self) -> list[ConsigliereMemo]:
        from src.core.models import Trade, TradeType

        memos = []

        with get_db() as db:
            recent = db.query(Trade).filter(
                Trade.trade_type == TradeType.SELL_PUT,
                Trade.delta_at_entry.isnot(None),
            ).order_by(Trade.created_at.desc()).limit(50).all()

        if len(recent) < 10:
            return memos

        # Average delta
        avg_delta = sum(abs(t.delta_at_entry or 0) for t in recent) / len(recent)

        if avg_delta > 0.25:
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="suggestion",
                title=f"Avg entry delta {avg_delta:.2f} — consider lowering delta_max",
                body=(
                    f"Average entry delta is {avg_delta:.3f} across {len(recent)} trades. "
                    f"This is above the 0.15-0.22 sweet spot. "
                    f"Reduce delta_max in settings.yaml by 0.02-0.03."
                ),
                metric_name="avg_entry_delta",
                metric_value=avg_delta,
                metric_benchmark=0.20,
            ))
        elif avg_delta < 0.12:
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="info",
                title=f"Avg entry delta {avg_delta:.2f} — very conservative",
                body=(
                    f"Average delta of {avg_delta:.3f} means very safe trades but "
                    f"low premiums. If win rate is above 85%, raising delta_min "
                    f"by 0.02 would increase income without much added risk."
                ),
                metric_name="avg_entry_delta",
                metric_value=avg_delta,
                metric_benchmark=0.20,
            ))

        # VIX regime awareness
        vix_entries = [t.vix_at_entry for t in recent if t.vix_at_entry]
        if len(vix_entries) >= 5:
            avg_vix = sum(vix_entries) / len(vix_entries)
            low_vix = [v for v in vix_entries if v < 15]

            if len(low_vix) > len(vix_entries) * 0.6:
                memos.append(ConsigliereMemo(
                    category="improvement",
                    severity="info",
                    title=f"Mostly low-VIX entries (avg {avg_vix:.1f})",
                    body=(
                        f"{len(low_vix)} of {len(vix_entries)} trades at VIX <15. "
                        f"Premiums are compressed in low-vol. Consider raising "
                        f"the iv_rank_min threshold during low-VIX periods "
                        f"to only trade the highest-IV stocks."
                    ),
                    metric_name="avg_entry_vix",
                    metric_value=avg_vix,
                    metric_benchmark=18.0,
                ))

        return memos
