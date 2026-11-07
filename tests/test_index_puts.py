"""Phase 2 (2026-11-04): cash-settled index put sleeve — pure helpers, config, sync tracking."""
import math
from datetime import date

from src.core.config import IndexPutConfig, Settings
from src.strategy.index_puts import (contracts_for_budget, daily_sigma, index_put_strike,
                                     margin_proxy_per_contract, pick_expiry, settlement_pnl,
                                     stop_triggered, POSITION_TYPE)


def test_ships_disabled_and_dry_run():
    c = IndexPutConfig()
    assert c.enabled is False and c.dry_run is True
    assert (c.symbol, c.exchange, c.dte, c.sigma_k, c.budget_pct, c.stop_mult) == ("XSP", "CBOE", 1, 2.0, 0.30, 3.0)
    assert Settings().index_puts.enabled is False


def test_sigma_and_strike_match_the_sim_rule():
    closes = [100 * (1.01 if i % 2 else 0.99) ** 1 for i in range(30)]
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] * (1.01 if i % 2 else 0.99))
    s = daily_sigma(closes, 20)
    assert s is not None and 0.009 < s < 0.011
    # spot 680, σ 1%, k 2, 1 DTE → 2% OTM → 666.4 → grid 666
    assert index_put_strike(680.0, 0.01, dte=1, sigma_k=2.0, otm_floor=0.01) == 666.0
    # the floor binds when σ is tiny
    assert index_put_strike(680.0, 0.001, dte=1, sigma_k=2.0, otm_floor=0.01) == math.floor(680 * 0.99)
    assert daily_sigma([100, 101], 20) is None


def test_budget_sizing_and_margin_proxy():
    m = margin_proxy_per_contract(680.0, 666.0, 0.15)      # 15%×680 − 14 = 88 → $8,800
    assert abs(m - 8800.0) < 1e-6
    assert contracts_for_budget(205_000, 0.30, m, 0.0, 50) == 6      # 61,500 / 8,800
    assert contracts_for_budget(205_000, 0.30, m, 61_000.0, 50) == 0  # budget used up
    assert contracts_for_budget(205_000, 0.30, m, 0.0, 3) == 3        # max_contracts cap
    assert contracts_for_budget(0, 0.30, m, 0.0, 50) == 0


def test_settlement_and_stop():
    intrinsic, realized = settlement_pnl(1.20, 666.0, 660.0, 5, commission_total=5.0)
    assert intrinsic == 3000.0 and realized == 1.20 * 500 - 3000 - 5
    intrinsic, realized = settlement_pnl(1.20, 666.0, 670.0, 5)
    assert intrinsic == 0.0 and realized == 600.0
    assert stop_triggered(3.6, 1.20, 3.0) is True
    assert stop_triggered(3.5, 1.20, 3.0) is False
    assert stop_triggered(None, 1.20, 3.0) is False
    assert stop_triggered(9.0, 1.20, 0.0) is False


def test_pick_expiry_counts_trading_days():
    exps = ["20261104", "20261105", "20261106", "20261109"]
    # Wednesday 2026-11-04, 1 DTE → Thursday
    assert pick_expiry(exps, date(2026, 11, 4), 1) == "20261105"
    # Friday 2026-11-06, 1 DTE → Monday
    assert pick_expiry(exps, date(2026, 11, 6), 1) == "20261109"
    assert pick_expiry(["20261104"], date(2026, 11, 6), 1) is None


def test_position_type_is_tracked_by_sync_but_not_by_the_wheel():
    import inspect
    from src.broker import trade_sync
    from src.strategy import wheel, profit_taker
    assert POSITION_TYPE in inspect.getsource(trade_sync)
    # the wheel's assignment detection and the put profit taker only look at short_put
    assert POSITION_TYPE not in inspect.getsource(wheel)
    assert POSITION_TYPE not in inspect.getsource(profit_taker)


def test_wing_helpers():
    from src.strategy.index_puts import (wing_strike, spread_max_loss_per_contract, contracts_for_max_loss,
                                         spread_settlement_pnl)
    # 3% under 666 → 646.02 → grid 646; snapped to a 5-point chain → 645
    assert wing_strike(666.0, 0.03) == 646.0
    assert wing_strike(666.0, 0.03, strikes=[640.0, 645.0, 650.0]) == 645.0
    # width 20, credit 1.20 → max loss $1,880 per spread
    assert abs(spread_max_loss_per_contract(666.0, 646.0, 1.20) - 1880.0) < 1e-6
    # 10% of $205k = $20.5k → 10 spreads; budget already used → 0; cap respected
    assert contracts_for_max_loss(205_000, 0.10, 1880.0, 0.0, 50) == 10
    assert contracts_for_max_loss(205_000, 0.10, 1880.0, 20_000.0, 50) == 0
    assert contracts_for_max_loss(205_000, 0.10, 1880.0, 0.0, 3) == 3
    # settlement: index at 630 → short pays 36, wing pays 16 → net loss capped at width − credit
    intr, wp, real = spread_settlement_pnl(1.20, 666.0, 646.0, 630.0, 2)
    assert intr == 7200.0 and wp == 3200.0 and real == 1.20 * 200 - 7200 + 3200
    assert real == -(20 - 1.20) * 200
    # no wing → behaves like the naked settlement
    intr, wp, real = spread_settlement_pnl(1.20, 666.0, None, 630.0, 2)
    assert wp == 0.0 and real == 1.20 * 200 - 7200


def test_spread_config_defaults():
    from src.core.config import IndexPutConfig
    c = IndexPutConfig()
    assert c.spread_enabled is True and c.spread_width_pct == 0.03 and c.max_loss_pct == 0.05
    assert c.min_net_credit == 0.10
