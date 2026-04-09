# Maggy & Winston — STATE

This file is updated at the end of every session.

## System Status (April 9, 2026)

Both connections stable. App running. http://37.0.30.34:8080

Screener: WORKING — 20 breakthrough / 65 growth / 15 dividend / 50 options
Last screener run: 2026-04-09 06:21 UTC — 100 stocks

## Top Priority Next Session

1. Verify portfolio health check fires every 5 min
2. Implement job_portfolio_monthly_review (CC harvesting + trailing stops)
3. Review 32 new watchlist stocks from screener

## What Changed (April 8-9, 2026)

- FMP 30-day cache added
- Screener targets: 20/65/15/50
- Screener and review split into separate jobs
- Breakthrough scan fixed: max_tokens 4000
- UNIQUE constraint and PLTR KeyError fixed
- Run Now button fixed: _ensure_event_loop + portfolio lock
- settings.yaml removed from git, API key rotated
- Portfolio health check: added _ensure_event_loop()

*Last updated: April 9, 2026*
