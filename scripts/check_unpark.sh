#!/bin/bash
# Did the XEON park sale (EUR runway restore) work today? Prints every un-park attempt, the
# FX-treasury debit-close, and the live EUR/USD cash. Usage: scripts/check_unpark.sh [YYYY-MM-DD]
cd "$(dirname "$0")/.."
DAY="${1:-$(date -u +%F)}"
echo "== un-park attempts on $DAY (UTC)"
cat logs/trader.log "logs/trader.log.$DAY" 2>/dev/null | grep -h "^$DAY" | grep -E "portfolio_unparked|portfolio_unpark_unfilled|portfolio_unpark_cancel_unconfirmed|portfolio_unpark_skipped|portfolio_unpark_error" \
  | sed -E 's/.*\] ([a-z_]+) +\[src[^]]*\] /\1  /' | cut -c1-160
echo "== FX treasury / funding on $DAY"
cat logs/trader.log "logs/trader.log.$DAY" 2>/dev/null | grep -h "^$DAY" | grep -E "fx_treasury_debit_close|fx_deferred|unfunded_skip|Could not fund" \
  | sed -E 's/.*\] ([a-z_]+) +\[[^]]*\] /\1  /' | cut -c1-200 | tail -6
echo "== live cash (IBKR, read-only)"
PYTHONPATH=. .venv/bin/python - <<'PY' 2>/dev/null
from ib_insync import IB
ib = IB(); ib.connect("127.0.0.1", 7496, clientId=197, readonly=True, timeout=15); ib.sleep(1)
for v in ib.accountValues():
    if v.tag == "CashBalance" and v.currency in ("EUR", "USD"): print(f"  {v.currency} {float(v.value):,.0f}")
for p in ib.positions():
    if p.contract.symbol == "XEON": print(f"  XEON {p.position:,.0f} shares")
ib.disconnect()
PY
