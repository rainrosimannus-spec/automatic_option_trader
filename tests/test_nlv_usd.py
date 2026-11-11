"""NLV currency fix (Rain 2026-11-11): the account is EUR-base, strikes/lots/collateral are USD.
Gates that compare NLV with dollar amounts read the USD figure; ratio gates (margin %, buying
power %, intraday loss) and the EUR dashboard are untouched."""
import re
from src.broker.account import AccountSummary, nlv_usd


def test_summary_usd_view_and_hand_built_default():
    s = AccountSummary(net_liquidation=192_649.0, buying_power=1_000.0, cash_balance=0, unrealized_pnl=0,
                       realized_pnl=0, maintenance_margin=60_000.0, fx_usd_per_base=1.116)
    assert round(s.net_liquidation_usd) == 214_996 and round(s.maintenance_margin_usd) == 66_960
    assert s.maintenance_margin_usd / s.net_liquidation_usd == s.maintenance_margin / s.net_liquidation
    plain = AccountSummary(net_liquidation=100.0, buying_power=0, cash_balance=0, unrealized_pnl=0,
                           realized_pnl=0, maintenance_margin=0)
    assert plain.net_liquidation_usd == 100.0 and nlv_usd(plain) == 100.0 and nlv_usd(None) == 0.0


def _func_bodies(src: str) -> dict:
    out, name, buf = {}, None, []
    for line in src.split("\n"):
        m = re.match(r"    def (\w+)\(", line)
        if m:
            if name: out[name] = "\n".join(buf)
            name, buf = m.group(1), []
        buf.append(line)
    if name: out[name] = "\n".join(buf)
    return out


def test_usd_gates_read_usd_and_ratio_gates_keep_base():
    bodies = _func_bodies(open("src/strategy/risk.py").read())
    for usd_gate in ("check_position_limit", "check_position_size", "check_total_exposure",
                     "check_daily_deployment", "check_delta_exposure", "check_sector_exposure"):
        assert "nlv_usd(summary)" in bodies[usd_gate], usd_gate
    for eur_gate in ("check_margin_usage", "check_buying_power", "check_intraday_loss"):
        assert "nlv_usd(summary)" not in bodies[eur_gate], eur_gate
    assert "nlv_usd" in open("src/strategy/put_seller.py").read()
    assert "nlv_usd" in open("src/scheduler/jobs.py").read()
    assert "net_liquidation_usd" in open("src/strategy/index_puts.py").read()
