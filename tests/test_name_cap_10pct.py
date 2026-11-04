"""Rule 5 (2026-11-04): per-name assignment notional cap 25% → 10% of NLV."""
from src.core.config import RiskConfig


def test_default_is_ten_percent():
    assert RiskConfig().max_single_name_notional_pct == 0.10


def test_affordable_contracts_follow_the_cap(monkeypatch):
    from src.strategy import risk as risk_mod
    from src.core.config import StrategyConfig
    monkeypatch.setattr(risk_mod, "get_settings",
                        lambda: type("S", (), {"risk": RiskConfig(), "strategy": StrategyConfig()})())
    # $205k NLV: a $150 strike → 10% = $20.5k → 1 lot; a $90 strike → 2 lots
    assert risk_mod.affordable_base_contracts(205_000, 150.0) == 1
    assert risk_mod.affordable_base_contracts(205_000, 90.0) == 2
    # below $100k the 50% branch is untouched
    assert risk_mod.adaptive_name_notional_pct(80_000) == 0.50
    assert risk_mod.adaptive_name_notional_pct(205_000) == 0.10


def test_one_lot_ceiling_default_and_gate(monkeypatch):
    from src.core.config import StrategyConfig
    from src.strategy import risk as risk_mod
    assert RiskConfig().max_single_name_one_lot_pct == 0.20
    rm = risk_mod.RiskManager.__new__(risk_mod.RiskManager)
    rm.cfg = RiskConfig()
    monkeypatch.setattr(risk_mod, "get_settings",
                        lambda: type("S", (), {"risk": RiskConfig(), "strategy": StrategyConfig()})())
    # replicate layer 3 arithmetic: $205k NLV, $350 strike → 1 lot = $35k = 17% → allowed by the
    # one-lot ceiling; $450 strike → $45k = 22% → over the ceiling
    nl = 205_000.0
    for price, ok in ((350.0, True), (450.0, False)):
        base = risk_mod.affordable_base_contracts(nl, price)
        notional = price * 100 * base
        cap = nl * risk_mod.adaptive_name_notional_pct(nl)
        one_lot = nl * rm.cfg.max_single_name_one_lot_pct
        allowed = (base == 1 and notional <= max(cap, one_lot)) or notional <= cap
        assert allowed is ok, price
