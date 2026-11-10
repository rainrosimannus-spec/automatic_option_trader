"""parse_park_realized: XEON realised P&L from the Flex Trades section (the /portfolio Park Realised card)."""
from src.portfolio.capital_injections import parse_park_realized

_HEAD = '<FlexQueryResponse><FlexStatements count="1"><FlexStatement accountId="U1" fromDate="20260601" toDate="20261110">'
_TAIL = '</FlexStatement></FlexStatements></FlexQueryResponse>'


def _trade(sym, side, qty, pnl, comm, date, level="EXECUTION", fx="1"):
    return (f'<Trade symbol="{sym}" assetCategory="STK" buySell="{side}" quantity="{qty}" '
            f'fifoPnlRealized="{pnl}" ibCommission="{comm}" tradeDate="{date}" '
            f'levelOfDetail="{level}" fxRateToBase="{fx}" currency="EUR"/>')


def test_sums_only_park_sells_at_execution_level():
    xml = _HEAD + "<Trades>" + "".join([
        _trade("XEON", "BUY", 2000, 0, -149.5, "20260701"),              # buys carry no realised
        _trade("XEON", "SELL", -1000, 1234.5, -74.9, "20260801"),
        _trade("XEON", "SELL", -500, 600.25, -37.4, "20260915"),
        _trade("XEON", "SELL", -1500, 1834.75, -112.3, "20260915", level="ORDER"),  # roll-up of the two above
        _trade("NVDA", "SELL", -10, 999.0, -1.0, "20260901"),           # other symbol
    ]) + "</Trades>" + _TAIL
    out = parse_park_realized(xml, "XEON")
    assert out["sells"] == 2 and out["shares_sold"] == 1500
    assert abs(out["realized"] - 1834.75) < 1e-9
    assert abs(out["commission"] + 112.3) < 1e-9
    assert out["first"] == "2026-08-01" and out["last"] == "2026-09-15"
    assert out["from"] == "2026-06-01" and out["to"] == "2026-11-10"


def test_no_trades_section_is_none_but_no_park_sale_is_zero():
    assert parse_park_realized(_HEAD + "<CashTransactions/>" + _TAIL, "XEON") is None
    out = parse_park_realized(_HEAD + "<Trades>" + _trade("NVDA", "SELL", -1, 5, 0, "20260901") + "</Trades>" + _TAIL, "XEON")
    assert out is not None and out["sells"] == 0 and out["realized"] == 0.0


def test_fx_to_base_applied():
    xml = _HEAD + "<Trades>" + _trade("XEON", "SELL", -1, 100.0, -2.0, "20260901", fx="0.5") + "</Trades>" + _TAIL
    out = parse_park_realized(xml, "XEON")
    assert out["realized"] == 50.0 and out["commission"] == -1.0
