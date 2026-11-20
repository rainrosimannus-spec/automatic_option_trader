"""Seed the crash-reserve spend counter (one-off, Rain runs it).

2026-11-20: tranche 1 fired and EUR 312k of US buys filled before the tranche accounting existed. The
base allowance that day was ~EUR 144k, so ~EUR 168k of it was reserve money. Without this seed Monday's
first scan would release the full one third (EUR 321k) again instead of the ~153k that is left.

    .venv/bin/python scripts/compounder_reserve_seed.py            # seeds 168000 for 2026-11-20
    .venv/bin/python scripts/compounder_reserve_seed.py 0          # reset (nothing spent)
"""
import sys
from datetime import datetime

from src.core.database import get_db
from src.portfolio.models import PortfolioState

spend_today = float(sys.argv[1]) if len(sys.argv) > 1 else 168000.0
spend_date = sys.argv[2] if len(sys.argv) > 2 else "2026-11-20"

with get_db() as db:
    for key, value in (("compounder_reserve_spent", "0"),
                       ("compounder_reserve_spend_today", str(round(spend_today))),
                       ("compounder_reserve_spend_date", spend_date)):
        row = db.query(PortfolioState).filter(PortfolioState.key == key).first()
        if row is None:
            db.add(PortfolioState(key=key, value=value, updated_at=datetime.utcnow()))
        else:
            row.value = value
            row.updated_at = datetime.utcnow()
    db.commit()
    print({r.key: r.value for r in db.query(PortfolioState)
           .filter(PortfolioState.key.like("compounder_reserve_spen%")).all()})
