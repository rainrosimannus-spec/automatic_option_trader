# Logging & diagnostics map — Maggy & Winston

> **Purpose:** when something breaks, know *exactly* where to look in under a
> minute. This file is the answer to "where does every subprocess write its
> output?" — the question that used to turn every incident into an hour of
> detective work.
>
> Last verified: 2026-05-25 on `octoserver-genoax2`.

---

## 1. The one-minute incident cheat-sheet

| Symptom | Look here first |
|---|---|
| Strategy / scheduler / IBKR misbehaving (orders, scans, assignments) | `logs/trader.log` (`grep` the symbol or job name) |
| **Web dashboard 500 / blank page / FastAPI error** | `logs/console.log` (uvicorn errors live here, **not** in trader.log) |
| Trader **crashed / won't start / died on import** | `logs/console.log` (captures stderr tracebacks + anything before logging init) |
| Trader got auto-restarted | `data/watchdog.log` |
| IBKR gateway / 2FA / login problems | `~/ibc/logs/portfolio/ibc-*_<Weekday>.txt` |
| Dashboard unreachable / TLS / auth / icon 401s | `/var/log/caddy/dashboard-access.log` (see RULES.md #18) |
| Screener run (monthly or "run now" button) | `logs/trader.log` (runs in-process; same place as scheduler) |

**Live tail during an incident:**
```bash
cd ~/automatic_option_trader
tail -f logs/console.log     # everything the process printed (web + crashes + dup of trader.log)
tail -f logs/trader.log      # structured strategy/scheduler logs only
```

---

## 2. Process topology — who runs what, and where output goes

Everything is started by tmux sessions + one cron watchdog. There is **no systemd
unit for the trader** (only Caddy is systemd-managed).

| Process | Launched by | tmux session | Structured log | stdout / stderr |
|---|---|---|---|---|
| **Trader** (`python -m src.main` — scheduler **+** web dashboard **+** screener, all one process) | `~/restart-all.sh`, respawned by `~/watchdog-trader.sh` | `trader` | `logs/trader.log` | tmux pane **+ `logs/console.log`** (via pipe-pane) |
| **IB Gateway** (Java, account U17562704, port 7496) | `~/start-gateway-portfolio.sh` | `portfolio` | `~/ibc/logs/portfolio/ibc-*.txt` | tmux pane (ephemeral); Xvfb → `/dev/null` |
| **Watchdog** | cron `*/5 * * * *` | — | `data/watchdog.log` | cron-swallowed (writes its own log) |
| **Caddy** (reverse proxy / TLS / basic-auth) | `systemd` (`caddy.service`) | — | `journalctl -u caddy` | `/var/log/caddy/dashboard-access.log` |
| **Lender portal** (son's — `lender.mesicap.com`) | separate uvicorn (son-owned) | — | `logs/lender.log` | son's domain — diagnose read-only, defer fixes |

---

## 3. The two trader logs — what each contains, and why both exist

`src/core/logger.py :: setup_logging()` configures the **root** stdlib logger with
**two** handlers:

1. A `StreamHandler` → **stdout** (`logging.basicConfig(stream=sys.stdout)`)
2. A `TimedRotatingFileHandler` → **`logs/trader.log`** (rotates at midnight, keeps 7)

structlog (colors off) routes through stdlib, so everything app-level lands in
**both** stdout and `trader.log`.

### The gap that made `trader.log` insufficient

`uvicorn.run(...)` (started in a thread in `src/main.py`) installs its **own**
loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`) with `propagate=False`.
They therefore **never reach the root file handler** → **web errors do not appear
in `trader.log`.** They only ever went to the process's stdout, i.e. the tmux
pane, which is **wiped on every restart**. Same fate for any traceback that
crashes the process *before* `setup_logging()` runs, or anything written to bare
stderr. That is the entire "web 500s only surfaced via direct Python invocation"
problem.

### The fix: `logs/console.log` (durable stdout/stderr capture)

`tmux pipe-pane` taps the `trader` pane's output stream to a file:

```bash
tmux pipe-pane -t trader 'cat >> /home/rain/automatic_option_trader/logs/console.log'
```

- **Zero risk to the trading process.** pipe-pane copies pane bytes out-of-band;
  the Python process never writes through the pipe, so even if the `cat` died the
  trader is unaffected (no SIGPIPE path — this is why we use pipe-pane and **not**
  `... | tee`).
- Captures **everything** the process prints, in order: the stdout duplicate of
  `trader.log` **plus** uvicorn/web errors **plus** pre-logging crashes.
- Wired into all three (re)start sites so it survives restarts:
  - `~/restart-all.sh` (after `tmux new-session -d -s trader`)
  - `~/watchdog-trader.sh` (both restart branches)
- Lines end with a trailing `\r` (CRLF — a pty artifact). `grep` works fine; for
  a clean copy use `sed 's/\r$//' logs/console.log`.

**Result:** `trader.log` = structured strategy/scheduler view. `console.log` =
raw everything-the-process-said, including web + crashes. One of the two always
has what you need.

---

## 4. Rotation

`logs/trader.log` self-rotates (Python `TimedRotatingFileHandler`, 7 days).

`logs/console.log` is rotated by **logrotate in user mode** (no sudo). Config:
`deploy/console-logrotate.conf` (daily, keep 14, `copytruncate` — required
because the `cat >>` pipe holds the file open). Installed as a daily cron line:

```cron
15 0 * * * /usr/sbin/logrotate -s /home/rain/.logrotate-console.state /home/rain/automatic_option_trader/deploy/console-logrotate.conf
```

Verify the config any time without changing anything:
```bash
/usr/sbin/logrotate -d -s /home/rain/.logrotate-console.state \
  /home/rain/automatic_option_trader/deploy/console-logrotate.conf
```

---

## 5. Known follow-up (optional, not yet done)

Route uvicorn's loggers into `trader.log` directly, so web errors are *structured*
and co-located even without reading `console.log`. Minimal change in `src/main.py`:
pass `log_config=None` to `uvicorn.run(...)` so its loggers propagate to root
(which has the file handler). **Requires a trader restart (2FA) to take effect**,
so it's deferred to a planned restart window. `console.log` already makes those
errors durable in the meantime.
