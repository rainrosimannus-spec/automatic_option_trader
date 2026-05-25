"""
Shared option-scoring core.

The Black-Scholes candidate selection used by the LIVE screener (screen_puts)
and the MarsWalk backtest, so both run identical logic. Extracted verbatim from
screener.py — the only change is that `today` is a parameter (live passes
datetime.now().date(); MarsWalk passes the simulation date).

PRICING NOTE: production scores on BS theoreticals, then fetches a live quote
for the actual order price (BS is for selection only). MarsWalk has no live
quote, so it uses the BS bid as the historical fill proxy — selection is
identical, fill price is the BS estimate.
"""
from __future__ import annotations

from datetime import datetime

from src.broker.greeks import compute_put_greeks, compute_call_greeks


def score_put_candidates(stock_price, iv, contracts, cfg, delta_min, delta_max,
                         resolved_dte_min, resolved_dte_max, today):
    """BS-score put contracts. Returns list[ScoredContract] (unsorted)."""
    from src.strategy.screener import ScoredContract, _weekend_theta_bonus  # noqa: F401
    candidates = []
    for contract in contracts:
        exp_date = datetime.strptime(contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
        dte = (exp_date - today).days
        if dte < 0:
            continue  # only skip expired options, not same-day

        # For DTE 0, use a small T so BS doesn't divide by zero
        T = max(dte, 0.25) / 365.0  # minimum ~6 hours of time value

        greeks = compute_put_greeks(stock_price, contract.strike, T, iv)
        if not greeks:
            continue

        delta = abs(greeks.delta)
        bid = greeks.bid
        ask = greeks.ask
        mid = greeks.mid

        # ── 0-4 DTE: fallback path — use strike distance instead of delta ──
        # BS delta is unreliable near expiry (gamma extremes).
        # Instead, filter by how far OTM the strike is as % of stock price.
        # Target: 3-10% OTM (e.g., stock at $186 → strikes $167-$180)
        if dte <= 3:
            otm_pct = (stock_price - contract.strike) / stock_price
            if otm_pct < 0.02 or otm_pct > 0.12:
                continue  # skip <2% OTM (too risky) or >12% OTM (no premium)
            eff_min_premium = max(getattr(cfg, 'min_premium_put', cfg.min_premium), 0.05)
            eff_min_bid = max(cfg.min_bid, 0.03)

            if mid < eff_min_premium:
                continue
            if bid < eff_min_bid:
                continue

            # ── Scoring components ──
            # 1. OTM distance: closer to 5% OTM = better (0-1)
            otm_target = 0.05
            otm_score = 1 - abs(otm_pct - otm_target) / otm_target
            otm_score = max(0, min(1, otm_score))

            # 2. Return on margin: premium relative to capital at risk (0-1)
            # Margin ~= 20% of strike * 100 (IBKR standard for short puts)
            margin_required = contract.strike * 100 * 0.20
            if margin_required > 0:
                rom = (mid * 100) / margin_required  # e.g. $0.50 premium / $3600 margin = 0.014
                # Normalize: 0.5% return = 0, 3%+ return = 1
                rom_score = min(1.0, max(0, (rom - 0.005) / 0.025))
            else:
                rom_score = 0

            # 3. Premium relative to stock price (0-1)
            # Normalizes across different price levels
            # 0.1% of stock price = 0, 1%+ = 1
            prem_pct = mid / stock_price if stock_price > 0 else 0
            premium_score = min(1.0, max(0, prem_pct / 0.01))

            # Final score: capital efficiency matters most
            # 35% OTM distance + 35% return-on-margin + 25% premium + 5% base
            dte_score = (3 - dte) / 3  # 0DTE=1.0, 1DTE=0.67, 2DTE=0.33, 3DTE=0.0
            score = (otm_score * 0.30) + (rom_score * 0.30) + (premium_score * 0.25) + (dte_score * 0.10) + 0.05
            if cfg.weekend_theta_enabled and cfg.weekend_theta_weight:
                score += cfg.weekend_theta_weight * _weekend_theta_bonus(today, exp_date, dte)

            candidates.append(ScoredContract(
                contract=contract,
                strike=contract.strike,
                expiry=contract.lastTradeDateOrContractMonth,
                delta=delta,  # keep BS delta for display even if unreliable
                bid=bid,
                ask=ask,
                mid=mid,
                iv=iv,
                open_interest=0,
                score=score,
            ))
            continue

        # ── 5-14 DTE: primary path (7 DTE target strategy) ──
        # Delta is reliable here — use it as the main filter.
        # Scoring prefers contracts closest to 7 DTE, good delta placement,
        # capital efficiency (ROM), and reasonable premium.
        if delta < delta_min or delta > delta_max:
            continue
        if mid < getattr(cfg, 'min_premium_put', cfg.min_premium):
            continue
        if bid < cfg.min_bid:
            continue

        # 1. DTE score — prefer midpoint of allowed DTE range, penalise both shorter and longer
        #    Midpoint of resolved_dte_min/max — no longer hardcoded to 7 DTE
        dte_target = max(1, (resolved_dte_min + resolved_dte_max) // 2)
        dte_score = 1 - abs(dte - dte_target) / max(dte_target, 1)
        dte_score = max(0.0, min(1.0, dte_score))

        # 2. Delta score — prefer centre of allowed range (~0.20-0.25)
        target_delta = (delta_min + delta_max) / 2
        delta_score = 1 - abs(delta - target_delta) / target_delta
        delta_score = max(0.0, min(1.0, delta_score))

        # 3. Return on margin — premium / capital at risk
        #    Normalised: 0.5% = 0, 3%+ = 1
        margin_required = contract.strike * 100 * 0.20
        rom = (mid * 100) / margin_required if margin_required > 0 else 0
        rom_score = min(1.0, max(0.0, (rom - 0.005) / 0.025))

        # 4. Premium as % of stock price — reduced weight vs 0-3 DTE
        #    Avoids chasing high premium from risky near-ATM strikes
        #    Normalised: 0.1% = 0, 1%+ = 1
        prem_pct = mid / stock_price if stock_price > 0 else 0
        premium_score = min(1.0, max(0.0, prem_pct / 0.01))

        # Final score: DTE proximity + delta quality + capital efficiency + premium
        score = (dte_score * 0.25) + (delta_score * 0.30) + (rom_score * 0.30) + (premium_score * 0.15)
        if cfg.weekend_theta_enabled and cfg.weekend_theta_weight:
            score += cfg.weekend_theta_weight * _weekend_theta_bonus(today, exp_date, dte)

        candidates.append(ScoredContract(
            contract=contract,
            strike=contract.strike,
            expiry=contract.lastTradeDateOrContractMonth,
            delta=delta,
            bid=bid,
            ask=ask,
            mid=mid,
            iv=iv,
            open_interest=0,
            score=score,
        ))
    return candidates


def score_call_candidates(stock_price, iv, contracts, cfg, cc_delta_min, cc_delta_max, today):
    """BS-score covered-call contracts. Returns list[ScoredContract] (unsorted).

    Extracted verbatim from screener.screen_calls (only `today` is a parameter).
    """
    from src.strategy.screener import ScoredContract  # noqa: F401
    candidates = []
    for contract in contracts:
        exp_date = datetime.strptime(contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
        dte = (exp_date - today).days
        if dte <= 0:
            continue

        T = dte / 365.0

        greeks = compute_call_greeks(stock_price, contract.strike, T, iv)
        if not greeks:
            continue

        delta = abs(greeks.delta)
        bid = greeks.bid
        ask = greeks.ask
        mid = greeks.mid

        if delta < cc_delta_min or delta > cc_delta_max:
            continue
        if mid < cfg.min_premium:
            continue
        if bid < cfg.min_bid:
            continue

        target_delta = (cc_delta_min + cc_delta_max) / 2
        delta_score = 1 - abs(delta - target_delta) / target_delta
        premium_score = mid
        score = (delta_score * 0.4) + (premium_score * 0.6)

        candidates.append(ScoredContract(
            contract=contract,
            strike=contract.strike,
            expiry=contract.lastTradeDateOrContractMonth,
            delta=delta,
            bid=bid,
            ask=ask,
            mid=mid,
            iv=iv,
            open_interest=0,
            score=score,
        ))
    return candidates
