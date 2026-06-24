# SKXHoldco → Standard Books bookkeeping bridge

End-of-day, cost-basis double-entry posting of the SKXHoldco IBKR account into
**Standard Books by Excellent** (HansaWorld REST API v2).

```
flex_extract.py  →  journal.py  →  standard_books.py
(IBKR Flex)         (translate)     (POST TRBlock / dry-run)
        \_________ daily_sync.py orchestrates _________/
```

**Status: DRY-RUN.** Out of the box it prints the exact balanced journals it
*would* post — nothing is sent until you add the connection block + chart of
accounts and pass `--live`.

## Quick start (dry-run)

```bash
# Replay the bundled sample statement (no IBKR/network needed):
PYTHONPATH=. .venv/bin/python -m src.bookkeeping.daily_sync --xml src/bookkeeping/sample_flex.xml

# Against the real SKXHoldco Flex query (once flex creds are set):
PYTHONPATH=. .venv/bin/python -m src.bookkeeping.daily_sync
```

Each journal prints as a T-account (Dr/Cr, balance check) plus the literal
`POST …?set_field…&set_row_field…` URL that would be sent.

## What it books

| IBKR event (Flex)                | Journal                                                        |
|----------------------------------|---------------------------------------------------------------|
| Trade BUY (STK/OPT)              | Dr Securities, Dr Commission, Cr Cash                         |
| Trade SELL                       | Dr Cash, Dr Commission, Cr Securities (cost), Cr/Dr Realized P&L (gross) |
| Trade CASH (FX, e.g. EUR.USD)    | Dr/Cr the two currency cash accounts, plug → FX gain/loss     |
| CashTransaction *Dividends*      | Dr Cash, Cr Dividend income                                   |
| CashTransaction *Withholding Tax*| Dr Withholding tax, Cr Cash                                   |
| CashTransaction *Interest*       | Dr Cash / Cr Interest income (or reverse if paid)            |
| CashTransaction *other/fees*     | Dr/Cr Fees                                                    |
| DepositWithdrawal                | Dr/Cr Cash vs Equity/Intercompany                            |

Cost basis only — no mark-to-market of open positions. Every entry is posted in
the company **base currency** (converted via IBKR's `fxRateToBase`), with the
original-currency amount kept in each row's narrative.

## Going live — what's still needed

1. **Add a `bookkeeping:` block to `config/settings.yaml`** (see
   `config.py` docstring for the full template):

   ```yaml
   bookkeeping:
     enabled: true
     dry_run: false
     base_currency: EUR
     flex:
       token: "..."          # SKXHoldco Flex Web Service token
       query_id: "..."       # query MUST include Trades, Cash Transactions,
                             # and Deposits & Withdrawals sections
     standard_books:
       base_url: "http://HOST:PORT"
       company: "1"
       username: "..."       # REST user: Full REST-API access + write to TRBlock
       password: "..."
     accounts:               # the real GL account numbers in your Standard Books
       securities: "1810"
       commission: "5610"
       realized_pnl: "6500"
       dividend_income: "6100"
       withholding_tax: "1760"
       interest_income: "6200"
       interest_expense: "5620"
       fees: "5600"
       fx_gain_loss: "6300"
       equity: "2510"
       cash: { USD: "1910", EUR: "1900" }
   ```

   On the Standard Books server: *System → Settings → Optional Features* →
   tick **Allow Basic HTTP Authentication** and **Web REST API**; give the REST
   user **Full** access to the *REST API* action and **write** to the
   Transaction register.

2. **Reconcile the TRBlock field names.** The header/row field names in
   `standard_books.py` (`TransDate`, `Comment`, `RefStr`; row `Account`,
   `Debit`, `Credit`, `Stp`) are the standard HansaWorld Transaction fields but
   can vary by version. Dump a real record and check:

   ```bash
   PYTHONPATH=. .venv/bin/python -m src.bookkeeping.daily_sync --describe
   ```

3. **Validate FX.** The FX (CASH-trade) path is simplified and flagged in code —
   verify it against a real SKXHoldco currency conversion before trusting it.

4. **Dry-run against real data**, eyeball the journals, then `--live`.

## Idempotency

Every journal carries its IBKR external id as the transaction reference
(`RefStr`). In `--live` mode, posted refs are recorded in
`data/bookkeeping_posted.jsonl`, so re-running a day never double-books.
Dry-run never writes the ledger.

## Scheduling

Once verified live, run `run_daily_sync()` once per day after the Flex statement
is available (Flex "Last Business Day" updates overnight). Wire it into
`src/scheduler/jobs.py` like the other daily jobs, or a cron line handed to the
user (cron edits are user-run — see project memory).

## Tests

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_bookkeeping.py -q
```
