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
            ("execution_quality", self._review_execution_quality),
            # New $-impact-ranked modules (May 2026)
            ("deployment_utilization", self._review_deployment_utilization),
            ("stuck_wheel", self._review_stuck_wheel),
            ("execution_quality_trend", self._review_execution_quality_trend),
            ("regime_aware_config", self._review_regime_aware_config),
            ("per_name_attribution", self._review_per_name_attribution),
            ("live_vs_backtest", self._review_live_vs_backtest),
            ("forward_event_risk", self._review_forward_event_risk),
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

    # ══════════════════════════════════════════════════════════
    # MODULE 7: EXECUTION QUALITY
    # How well do our fills capture the bid-ask spread?
    # Compares each fill against the decision-time quote snapshot.
    # ══════════════════════════════════════════════════════════

    def _review_execution_quality(self) -> list[ConsigliereMemo]:
        from src.core.models import Trade, TradeType, OrderStatus

        memos = []

        with get_db() as db:
            recent = db.query(Trade).filter(
                Trade.trade_type.in_([TradeType.SELL_PUT, TradeType.SELL_CALL]),
                Trade.order_status == OrderStatus.FILLED,
                Trade.mid_at_entry.isnot(None),
                Trade.fill_price > 0,
            ).order_by(Trade.created_at.desc()).limit(100).all()

        # Keep only rows with a usable spread snapshot.
        samples = []
        for t in recent:
            bid, ask, mid, fill = t.bid_at_entry, t.ask_at_entry, t.mid_at_entry, t.fill_price
            if bid is None or ask is None or mid is None:
                continue
            spread = ask - bid
            if spread <= 0 or mid <= 0:
                continue
            # We SELL premium, so filling toward the ask is good, toward the bid
            # is bad. capture: 0.0 = filled at bid, 0.5 = at mid, 1.0 = at ask.
            capture = (fill - bid) / spread
            slip_vs_mid = mid - fill           # >0 = sold below fair (mid) value
            slip_pct = slip_vs_mid / mid * 100
            dollars = slip_vs_mid * 100 * (t.quantity or 1)  # per-share → per-contract
            samples.append((capture, slip_pct, dollars))

        n = len(samples)
        if n < 10:
            return memos

        avg_capture = sum(s[0] for s in samples) / n
        avg_slip_pct = sum(s[1] for s in samples) / n
        total_dollars = sum(s[2] for s in samples)

        # Always record the metric (visible in trader.log even when execution is fine).
        log.info(
            "consigliere_execution_quality",
            n=n,
            avg_spread_capture=round(avg_capture, 3),
            avg_slip_pct_of_mid=round(avg_slip_pct, 2),
            total_dollars_vs_mid=round(total_dollars, 2),
        )

        # Speak up only when execution is measurably poor — fills landing in the
        # bottom of the spread (selling at the bid → capture ≈ 0). Matches the
        # "only flag real issues" ethos of the other modules.
        if avg_capture < 0.40:
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="suggestion",
                title=f"Execution: capturing {avg_capture * 100:.0f}% of the bid-ask spread",
                body=(
                    f"Across the last {n} premium-sells with a quote snapshot, fills "
                    f"landed on average {avg_capture * 100:.0f}% of the way from bid to "
                    f"ask (0% = at the bid, 50% = at the mid). That gave up about "
                    f"{avg_slip_pct:.1f}% of fair (mid) value per trade — roughly "
                    f"${abs(total_dollars):,.0f} across the sample. "
                    f"Orders are placed as limits at the bid to guarantee fills; on "
                    f"liquid names a mid-or-slightly-better limit with a short reprice "
                    f"window would recover much of this without materially hurting the "
                    f"fill rate. Observation only — no order was changed."
                ),
                metric_name="spread_capture_pct",
                metric_value=round(avg_capture * 100, 1),
                metric_benchmark=50.0,
            ))

        return memos

    # ══════════════════════════════════════════════════════════
    # META-FRAMEWORK — dollarize / speak-on-change / n-confidence
    # All new modules go through these helpers so memos triage by money,
    # don't repeat the same finding every day, and disclose sample size.
    # ══════════════════════════════════════════════════════════

    def _confidence_label(self, n: int) -> str:
        """Sample-size honesty: thin samples are explicitly down-ranked."""
        if n < 10:
            return "low"
        if n < 30:
            return "medium"
        return "high"

    def _recent_memo(self, metric_name: str, days: int = 7):
        """Most recent memo with this metric_name in the last `days` (or None)."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_db() as db:
            from sqlalchemy import desc
            return (db.query(ConsigliereMemo)
                    .filter(ConsigliereMemo.metric_name == metric_name)
                    .filter(ConsigliereMemo.created_at >= cutoff)
                    .order_by(desc(ConsigliereMemo.created_at))
                    .first())

    def _should_speak(self, metric_name: str, value: float,
                      worse_when: str = "higher", days: int = 7,
                      worsen_pct: float = 0.10) -> bool:
        """
        Speak-on-change gate: emit only when this is new, the metric has
        materially worsened vs the last memo, or the previous one is old.

        worse_when='higher' → metric is worse when bigger (e.g. % deployed gap,
                              concentration). 'lower' → smaller is worse
                              (e.g. spread capture %).
        """
        last = self._recent_memo(metric_name, days=days)
        if not last or last.metric_value is None:
            return True  # first observation in the window
        if worse_when == "higher":
            return value > last.metric_value * (1.0 + worsen_pct)
        return value < last.metric_value * (1.0 - worsen_pct)

    def _live_nlv(self) -> float | None:
        """Live NLV from the IBKR account cache, or None if unavailable."""
        try:
            from src.broker.account import get_account_summary
            acct = get_account_summary()
            return acct.net_liquidation if acct and acct.net_liquidation > 0 else None
        except Exception:
            return None

    def _live_cap_pct(self, nlv: float) -> float:
        """Mirror risk._effective_total_exposure_pct: NLV-ramp 20/25/30%."""
        try:
            from src.core.config import get_settings
            base = get_settings().risk.total_exposure_pct
        except Exception:
            base = 0.20
        if nlv >= 4_000_000:
            return max(0.30, base)
        if nlv >= 2_000_000:
            return max(0.25, base)
        return base

    # ══════════════════════════════════════════════════════════
    # MODULE 8: CAPITAL-DEPLOYMENT UTILIZATION
    # The biggest direct-cash lever — deployed vs allowed cap, dollarized.
    # ══════════════════════════════════════════════════════════

    def _review_deployment_utilization(self) -> list[ConsigliereMemo]:
        from src.core.models import Position, PositionStatus

        memos = []
        nlv = self._live_nlv()
        if not nlv:
            return memos

        cap_pct = self._live_cap_pct(nlv)
        cap_dollars = nlv * cap_pct

        with get_db() as db:
            puts = db.query(Position).filter(
                Position.status == PositionStatus.OPEN,
                Position.position_type == "short_put",
            ).all()

        deployed = sum((p.strike or 0) * (p.quantity or 1) * 100 for p in puts)
        if cap_dollars <= 0:
            return memos
        utilization = deployed / cap_dollars   # 0..1 of the cap
        deployed_pct = deployed / nlv * 100

        # Headline: ran at U% of an C% cap → premium left on the table.
        gap_dollars = max(0.0, cap_dollars - deployed)
        # Assume ~1.5% / month average put-premium yield on collateral (conservative;
        # the wheel's historical premium-yield-on-collateral runs ~1-2% monthly).
        premium_left = gap_dollars * 0.015

        # Speak only when utilization is meaningfully low.
        if utilization < 0.75 and self._should_speak(
            "deployment_utilization_pct", utilization * 100,
            worse_when="lower", worsen_pct=0.10,
        ):
            # Attribution candidates — these are causes the live system can produce,
            # not measured root-cause analysis; surfaced so the operator can correlate.
            causes = []
            if len(puts) == 0:
                causes.append("no open short-put positions — scanner not finding signals")
            else:
                causes.append(f"{len(puts)} open short-put positions filling ~{deployed_pct:.0f}% of NLV")
            try:
                from src.core.config import get_settings
                opt_count = getattr(get_settings().strategy, "options_count", 50)
                causes.append(f"universe scope ≈ top {opt_count} names by score")
            except Exception:
                pass
            causes.append("check screener_reject log for stale-data / no-IBKR-data days")
            cause_text = "; ".join(causes)

            memos.append(ConsigliereMemo(
                category="improvement",
                severity="suggestion" if utilization < 0.5 else "info",
                title=f"Deployed {deployed_pct:.0f}% of NLV against a {cap_pct*100:.0f}% cap",
                body=(
                    f"Put collateral is ${deployed:,.0f} against a cap of ${cap_dollars:,.0f} "
                    f"({utilization*100:.0f}% utilization). At the conservative ~1.5%/mo "
                    f"premium-yield-on-collateral, the ${gap_dollars:,.0f} unused cap is "
                    f"roughly ${premium_left:,.0f}/mo of premium left on the table. "
                    f"Candidate causes: {cause_text}. "
                    f"Diagnostic — no parameter is changed automatically."
                ),
                metric_name="deployment_utilization_pct",
                metric_value=round(utilization * 100, 1),
                metric_benchmark=85.0,
                impact_eur_month=round(premium_left, 0),
                sample_n=len(puts),
                confidence="low" if len(puts) < 5 else "medium",
            ))

        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 9: STUCK-WHEEL / CAPITAL-TRAP DETECTOR
    # The biggest hidden loss — assigned stock you're trapped in, bleeding
    # while CCs can't clear cost basis.
    # ══════════════════════════════════════════════════════════

    def _review_stuck_wheel(self) -> list[ConsigliereMemo]:
        from src.core.models import Position, PositionStatus
        from src.broker.market_data import get_stock_price

        memos = []
        with get_db() as db:
            stocks = db.query(Position).filter(
                Position.status == PositionStatus.OPEN,
                Position.position_type == "stock",
            ).all()

        if not stocks:
            return memos

        cutoff = datetime.utcnow() - timedelta(days=60)
        stuck = []  # (symbol, shares, cost_basis, current_px, days_held, tied_dollars, gap_pct)
        for s in stocks:
            if not s.opened_at or s.opened_at > cutoff:
                continue
            if not s.cost_basis or not s.quantity or s.quantity <= 0:
                continue
            try:
                px = get_stock_price(s.symbol)
            except Exception:
                px = None
            if not px or px <= 0:
                continue
            net_cb = s.cost_basis - (s.total_premium_collected or 0) / max(1, s.quantity)
            gap_pct = (px - net_cb) / net_cb if net_cb > 0 else 0
            if gap_pct < -0.05:   # >=5% underwater after CC premium credit
                days_held = (datetime.utcnow() - s.opened_at).days
                tied = px * s.quantity            # current dead-money market value
                stuck.append((s.symbol, s.quantity, net_cb, px, days_held, tied, gap_pct))

        if not stuck:
            return memos

        # Aggregate dead-capital opportunity cost: monthly premium yield those
        # dollars could have earned if redeployed as fresh put collateral.
        total_tied = sum(r[5] for r in stuck)
        # ~1.5% / mo premium-yield-on-collateral (matches Module 8 assumption).
        opportunity_cost = total_tied * 0.015

        # Headline + per-symbol detail (top 5 by tied capital).
        stuck.sort(key=lambda r: -r[5])
        lines = []
        for sym, qty, cb, px, days, tied, gap in stuck[:5]:
            lines.append(
                f"  {sym}: {qty}sh @ ${cb:.2f} cb, now ${px:.2f} ({gap*100:+.1f}%), "
                f"held {days}d → ${tied:,.0f} tied"
            )

        if self._should_speak("stuck_wheel_count", float(len(stuck)),
                              worse_when="higher", worsen_pct=0.0):
            memos.append(ConsigliereMemo(
                category="risk",
                severity="warning",
                title=f"Stuck wheel: {len(stuck)} names trapped, ~${opportunity_cost:,.0f}/mo dead-capital cost",
                body=(
                    f"{len(stuck)} assigned positions held >60d with current price ≥5% "
                    f"below net cost basis (after CC premium credit). Top names:\n"
                    + "\n".join(lines)
                    + f"\nTotal tied capital ${total_tied:,.0f}. At ~1.5%/mo redeployed "
                    f"premium yield that's ~${opportunity_cost:,.0f}/mo of opportunity cost "
                    f"while waiting for these to recover. "
                    f"Advisory: review the wheel-exit policy on the longest-held / "
                    f"deepest-underwater names — letting them go at a small loss may "
                    f"compound faster than waiting for CC premium to close the gap."
                ),
                metric_name="stuck_wheel_count",
                metric_value=float(len(stuck)),
                metric_benchmark=0.0,
                impact_eur_month=round(opportunity_cost, 0),
                sample_n=len(stuck),
                confidence=self._confidence_label(len(stuck)),
            ))

        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 10: EXECUTION-QUALITY TREND + $/MONTH
    # Trends spread capture month-over-month; dollarizes monthly leak that
    # scales with deployment so the cost stays visible as the account grows.
    # ══════════════════════════════════════════════════════════

    def _review_execution_quality_trend(self) -> list[ConsigliereMemo]:
        from src.core.models import Trade, TradeType, OrderStatus

        memos = []
        now = datetime.utcnow()
        cur_start = now - timedelta(days=30)
        prv_start = now - timedelta(days=60)

        with get_db() as db:
            recent = db.query(Trade).filter(
                Trade.trade_type.in_([TradeType.SELL_PUT, TradeType.SELL_CALL]),
                Trade.order_status == OrderStatus.FILLED,
                Trade.mid_at_entry.isnot(None),
                Trade.fill_price > 0,
                Trade.created_at >= prv_start,
            ).all()

        def _samples(trades, start, end):
            out = []
            for t in trades:
                if not (start <= t.created_at < end):
                    continue
                bid, ask, mid, fill = t.bid_at_entry, t.ask_at_entry, t.mid_at_entry, t.fill_price
                if bid is None or ask is None or mid is None:
                    continue
                spread = ask - bid
                if spread <= 0 or mid <= 0:
                    continue
                capture = (fill - bid) / spread
                slip = (mid - fill) * 100 * (t.quantity or 1)  # $ given up vs mid (per contract)
                out.append((capture, slip))
            return out

        cur = _samples(recent, cur_start, now)
        prv = _samples(recent, prv_start, cur_start)
        if len(cur) < 5:
            return memos  # not enough to trend

        cur_cap = sum(s[0] for s in cur) / len(cur)
        cur_leak = sum(s[1] for s in cur)
        # Annualize by trade-rate: extrapolate this month's leak to a monthly figure.
        leak_per_month = cur_leak * (30 / 30)  # already a 30-day window

        prv_cap = sum(s[0] for s in prv) / len(prv) if prv else None
        trend_txt = ""
        worsened = False
        if prv_cap is not None and len(prv) >= 5:
            delta = (cur_cap - prv_cap) * 100
            arrow = "↑" if delta > 0 else "↓"
            trend_txt = f" vs {prv_cap*100:.0f}% prior month ({arrow}{abs(delta):.0f}pp)"
            worsened = delta < -3.0

        # Speak when: worsening OR persistently bad. Skip when fine and stable.
        speak = worsened or cur_cap < 0.40
        if speak and self._should_speak(
            "execution_capture_monthly", cur_cap * 100,
            worse_when="lower", worsen_pct=0.05,
        ):
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="warning" if worsened else "suggestion",
                title=(
                    f"Execution trend: {cur_cap*100:.0f}% spread capture this month"
                    f"{trend_txt}, ~${abs(leak_per_month):,.0f}/mo leak"
                ),
                body=(
                    f"Across {len(cur)} premium-sells the last 30d, fills landed "
                    f"{cur_cap*100:.0f}% of the way bid→ask{trend_txt}. "
                    f"That gave up ~${abs(leak_per_month):,.0f}/mo vs filling at mid. "
                    f"This $-leak scales linearly with deployment, so the cost will "
                    f"grow as the NLV ramps — fix the limit-pricing policy before "
                    f"scaling. Mid-or-slightly-better limit with a short reprice "
                    f"window typically recovers most of the leak on liquid names. "
                    f"Observation only — orders are unchanged."
                ),
                metric_name="execution_capture_monthly",
                metric_value=round(cur_cap * 100, 1),
                metric_benchmark=50.0,
                impact_eur_month=round(abs(leak_per_month), 0),
                sample_n=len(cur),
                confidence=self._confidence_label(len(cur)),
            ))
        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 11: REGIME-AWARE CONFIG (bridge to MarsWalk)
    # Classify the live regime and surface MarsWalk's best-config-for-category.
    # ══════════════════════════════════════════════════════════

    def _review_regime_aware_config(self) -> list[ConsigliereMemo]:
        memos = []
        # Read current VIX from system_state (set by the live state writer).
        try:
            from src.core.models import SystemState
            with get_db() as db:
                row = db.query(SystemState).filter_by(key="vix").first()
            vix = float(row.value) if row and row.value else None
        except Exception:
            vix = None
        if vix is None:
            return memos

        # Simple live-regime classifier (matches the MarsWalk regime categories).
        if vix > 28:
            live_category, label = "crash", "vol spike / crash"
        elif vix < 14:
            live_category, label = "breakthrough", "low-vol grind"
        elif vix < 20:
            live_category, label = "sideways", "choppy / sideways"
        else:
            live_category, label = "whipsaw", "elevated / whipsaw"

        # Best MarsWalk run for the matching category.
        try:
            from src.marswalk.models import get_mw_db, Run
            with get_mw_db() as db:
                runs = db.query(Run).filter(Run.category == live_category).all()
        except Exception:
            return memos
        if not runs:
            return memos

        best = max(runs, key=lambda r: r.final_return_pct)
        best_avg = sum(r.final_return_pct for r in runs) / len(runs)

        # Speak once per week per regime category — skip if already advised
        # about this same regime in the last 7 days.
        metric_key = f"regime_aware_{live_category}"
        if not self._recent_memo(metric_key, days=7):
            memos.append(ConsigliereMemo(
                category="strategy",
                severity="info",
                title=f"Regime ≈ {label} (VIX {vix:.1f}); best MarsWalk config: dte {best.dte_min}–{best.dte_max}, Δ {best.delta_min:.2f}–{best.delta_max:.2f}",
                body=(
                    f"Live regime classified as '{label}' (VIX {vix:.1f}). MarsWalk has "
                    f"{len(runs)} run(s) in the matching '{live_category}' category; "
                    f"best returned {best.final_return_pct:+.1f}% on {best.regime_name} "
                    f"using DTE {best.dte_min}-{best.dte_max}, delta "
                    f"{best.delta_min:.2f}-{best.delta_max:.2f}. "
                    f"Average across category: {best_avg:+.1f}%. "
                    f"Advisory — confidence depends on backtest count; rerun MarsWalk "
                    f"to widen the sample before changing live parameters."
                ),
                metric_name="regime_best_return_pct",
                metric_value=round(best.final_return_pct, 1),
                metric_benchmark=24.0,
                sample_n=len(runs),
                confidence=self._confidence_label(len(runs)),
            ))
        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 12: PER-NAME P&L ATTRIBUTION → EVICTION CANDIDATES
    # Which names create vs trap capital? Feed eviction with evidence.
    # ══════════════════════════════════════════════════════════

    def _review_per_name_attribution(self) -> list[ConsigliereMemo]:
        from src.core.models import Position, PositionStatus

        memos = []
        cutoff = datetime.utcnow() - timedelta(days=180)
        with get_db() as db:
            closed = db.query(Position).filter(
                Position.status.in_([PositionStatus.CLOSED, PositionStatus.EXPIRED]),
                Position.opened_at >= cutoff,
            ).all()

        if len(closed) < 10:
            return memos

        agg: dict[str, dict] = {}
        for p in closed:
            a = agg.setdefault(p.symbol, {"pnl": 0.0, "n": 0})
            a["pnl"] += p.realized_pnl or 0
            a["n"] += 1

        losers = sorted(
            [(sym, d["pnl"], d["n"]) for sym, d in agg.items() if d["pnl"] < -500 and d["n"] >= 2],
            key=lambda r: r[1],
        )
        if not losers:
            return memos

        worst_sum = sum(r[1] for r in losers[:5])
        lines = [f"  {sym}: ${pnl:,.0f} over {n} cycles" for sym, pnl, n in losers[:5]]
        n_sample = sum(d["n"] for d in agg.values())

        if self._should_speak("per_name_loser_count", float(len(losers)),
                              worse_when="higher", worsen_pct=0.20):
            memos.append(ConsigliereMemo(
                category="improvement",
                severity="suggestion",
                title=f"{len(losers)} symbols net-losers over 180d (top 5: ${worst_sum:,.0f})",
                body=(
                    f"Per-name P&L attribution across {n_sample} closed cycles "
                    f"surfaces {len(losers)} symbols with >$500 net loss and ≥2 "
                    f"completed cycles. Worst:\n" + "\n".join(lines) +
                    f"\nAdvisory: these are eviction candidates for the discovered "
                    f"pool — recurring losers should require explicit justification "
                    f"to stay in the universe."
                ),
                metric_name="per_name_loser_count",
                metric_value=float(len(losers)),
                metric_benchmark=0.0,
                impact_eur_month=round(abs(worst_sum) / 6.0, 0),  # 180d sample → ~/month
                sample_n=n_sample,
                confidence=self._confidence_label(n_sample),
            ))
        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 13: LIVE-VS-BACKTEST DIVERGENCE WATCHDOG
    # Watch the live YTD curve against the backtest expectation for the
    # current regime — flag unexplained drift.
    # ══════════════════════════════════════════════════════════

    def _review_live_vs_backtest(self) -> list[ConsigliereMemo]:
        memos = []
        nlv = self._live_nlv()
        if not nlv:
            return memos

        # Live YTD return — use realized P&L since Jan 1 as a proxy (no daily snapshot).
        from src.core.models import Position, PositionStatus
        ytd_start = datetime(datetime.utcnow().year, 1, 1)
        with get_db() as db:
            closed = db.query(Position).filter(
                Position.status.in_([PositionStatus.CLOSED, PositionStatus.EXPIRED]),
                Position.closed_at >= ytd_start,
            ).all()
        if not closed:
            return memos
        live_pnl = sum(p.realized_pnl or 0 for p in closed)
        live_pct = live_pnl / nlv * 100

        # Backtest expectation: average final_return_pct from MarsWalk runs
        # in any "neutral/sideways/breakthrough" category as a baseline.
        try:
            from src.marswalk.models import get_mw_db, Run
            with get_mw_db() as db:
                runs = db.query(Run).filter(
                    Run.category.in_(["sideways", "breakthrough"])
                ).all()
        except Exception:
            return memos
        if not runs:
            return memos
        # MarsWalk runs are full-window returns, not annualized. Scale to YTD fraction.
        ytd_frac = (datetime.utcnow() - ytd_start).days / 365.0
        backtest_expected = (sum(r.final_return_pct for r in runs) / len(runs)) * ytd_frac
        drift = live_pct - backtest_expected

        if abs(drift) > 5.0 and self._should_speak(
            "live_vs_backtest_drift_pct", abs(drift),
            worse_when="higher", worsen_pct=0.20,
        ):
            direction = "above" if drift > 0 else "below"
            memos.append(ConsigliereMemo(
                category="performance",
                severity="info" if drift > 0 else "suggestion",
                title=f"Live YTD {live_pct:+.1f}% is {abs(drift):.1f}pp {direction} backtest baseline",
                body=(
                    f"Live YTD realized P&L is {live_pct:+.1f}% of NLV (proxy: closed "
                    f"positions since {ytd_start.date()}). MarsWalk's non-crash regime "
                    f"baseline scaled to YTD is {backtest_expected:+.1f}%. "
                    f"Drift {drift:+.1f}pp. "
                    f"Possible causes: selection drift, execution leak, leverage delta "
                    f"(live uses margin, backtest is cash-secured), or a regime MarsWalk "
                    f"didn't cover. Self-diagnostic — no action."
                ),
                metric_name="live_vs_backtest_drift_pct",
                metric_value=round(abs(drift), 1),
                metric_benchmark=0.0,
                sample_n=len(runs),
                confidence=self._confidence_label(len(runs)),
            ))
        return memos

    # ══════════════════════════════════════════════════════════
    # MODULE 14: FORWARD EVENT-RISK AGGREGATION
    # The earnings gate is reactive (per-entry). This looks ahead and
    # aggregates exposure — concentrated gap risk you can act on.
    # ══════════════════════════════════════════════════════════

    def _review_forward_event_risk(self) -> list[ConsigliereMemo]:
        memos = []
        from src.core.models import Position, PositionStatus

        try:
            from src.broker.market_data import has_upcoming_earnings
        except Exception:
            return memos

        with get_db() as db:
            puts = db.query(Position).filter(
                Position.status == PositionStatus.OPEN,
                Position.position_type == "short_put",
            ).all()

        if not puts:
            return memos

        exposed = []  # (symbol, strike, qty, days_to_expiry)
        for p in puts:
            if not p.expiry or not p.strike:
                continue
            try:
                exp_d = datetime.strptime(p.expiry, "%Y%m%d").date()
                dte = (exp_d - datetime.utcnow().date()).days
                if dte <= 0:
                    continue
                if has_upcoming_earnings(p.symbol, within_days=dte):
                    exposed.append((p.symbol, p.strike, p.quantity or 1, dte))
            except Exception:
                continue

        if not exposed:
            return memos

        # Dollarize concentrated gap risk: estimate ~5% adverse gap on each
        # earnings name × notional (industry rule of thumb for earnings move).
        gap_dollar_risk = sum(s * q * 100 * 0.05 for _, s, q, _ in exposed)
        names = ", ".join(sorted({e[0] for e in exposed})[:8])

        if self._should_speak("forward_earnings_exposure_count", float(len(exposed)),
                              worse_when="higher", worsen_pct=0.0):
            memos.append(ConsigliereMemo(
                category="risk",
                severity="warning" if len(exposed) >= 5 else "suggestion",
                title=(
                    f"{len(exposed)} open puts cross earnings before expiry "
                    f"(~${gap_dollar_risk:,.0f} concentrated gap risk)"
                ),
                body=(
                    f"Names: {names}. Each has earnings before expiry — concentrated "
                    f"adverse-gap risk in a single window. Estimate assumes ~5% adverse "
                    f"move per exposed name × notional. The live earnings gate prevents "
                    f"NEW entries, but already-open puts are still exposed. Advisory: "
                    f"consider rolling the largest-notional names out past earnings "
                    f"or closing for a small credit."
                ),
                metric_name="forward_earnings_exposure_count",
                metric_value=float(len(exposed)),
                metric_benchmark=0.0,
                impact_eur_month=round(gap_dollar_risk, 0),  # gap-risk magnitude, not monthly
                sample_n=len(exposed),
                confidence=self._confidence_label(len(exposed)),
            ))
        return memos
