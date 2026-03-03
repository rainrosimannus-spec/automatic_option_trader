"""
Alerts — push notifications for critical events and daily summaries.

Supports multiple backends (configure one or more):
  1. Telegram Bot (free, recommended) — needs bot token + chat ID
  2. ntfy.sh (free, no signup) — just pick a topic name
  3. Console log (always on) — fallback

Alert types:
  🚨 CRITICAL: halt triggered, circuit breaker, connection lost >10min
  📊 DAILY:    end-of-day summary after US market close
  💰 TRADE:    put-entry assigned, bridge transfer executed
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from datetime import datetime, date
from typing import Optional

from src.core.logger import get_logger

log = get_logger(__name__)


class AlertConfig:
    """Alert configuration — set in settings.yaml or dashboard."""
    def __init__(
        self,
        enabled: bool = True,
        # Telegram
        telegram_enabled: bool = False,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        # ntfy.sh
        ntfy_enabled: bool = False,
        ntfy_topic: str = "",           # e.g., "my-options-trader-xyz"
        ntfy_server: str = "https://ntfy.sh",
        # What to alert on
        alert_critical: bool = True,     # halts, circuit breaker, disconnects
        alert_daily: bool = True,        # end-of-day summary
        alert_trades: bool = False,      # individual trades (noisy)
        alert_bridge: bool = True,       # bridge transfers
        alert_assignments: bool = True,  # put assignments (options + portfolio)
        alert_rescreen: bool = True,     # annual rescreen completion + review suggestions
        alert_suggestions: bool = True,  # new suggestions needing approval
    ):
        self.enabled = enabled
        self.telegram_enabled = telegram_enabled
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.ntfy_enabled = ntfy_enabled
        self.ntfy_topic = ntfy_topic
        self.ntfy_server = ntfy_server
        self.alert_critical = alert_critical
        self.alert_daily = alert_daily
        self.alert_trades = alert_trades
        self.alert_bridge = alert_bridge
        self.alert_assignments = alert_assignments
        self.alert_rescreen = alert_rescreen
        self.alert_suggestions = alert_suggestions


class AlertManager:
    """Send notifications via configured backends."""

    def __init__(self, cfg: AlertConfig):
        self.cfg = cfg

    # ── Public API ───────────────────────────────────────────

    def critical(self, title: str, message: str):
        """🚨 Critical alert — halt, circuit breaker, prolonged disconnect."""
        if not self.cfg.alert_critical:
            return
        full = f"🚨 {title}\n{message}"
        self._send(full, priority="urgent", tags="warning")

    def daily_summary(
        self,
        net_liq: float,
        daily_pnl: float,
        realized_pnl: float,
        unrealized_pnl: float,
        open_positions: int,
        trades_today: int,
        annual_return_pct: float | None = None,
        currency: str = "EUR",
        options_account: bool = True,
    ):
        """📊 End-of-day summary."""
        if not self.cfg.alert_daily:
            return

        account_type = "Options" if options_account else "Portfolio"
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        sign = "+" if daily_pnl >= 0 else ""

        lines = [
            f"📊 {account_type} Daily Summary — {date.today().strftime('%b %d')}",
            f"",
            f"Net Liquidation: {currency} {net_liq:,.2f}",
            f"Daily P&L:       {sign}{currency} {daily_pnl:,.2f} {pnl_emoji}",
            f"  Realized:      {currency} {realized_pnl:,.2f}",
            f"  Unrealized:    {currency} {unrealized_pnl:,.2f}",
            f"Open Positions:  {open_positions}",
            f"Trades Today:    {trades_today}",
        ]

        if annual_return_pct is not None:
            ann_emoji = "✅" if annual_return_pct >= 0 else "⚠️"
            lines.append(f"Annual Return:   {annual_return_pct:+.1f}% {ann_emoji}")

        self._send("\n".join(lines), priority="low", tags="chart_with_upwards_trend")

    def trade_alert(self, action: str, symbol: str, details: str):
        """💰 Trade notification."""
        if not self.cfg.alert_trades:
            return
        self._send(f"💰 {action}: {symbol}\n{details}", tags="moneybag")

    def assignment_alert(self, symbol: str, shares: int, strike: float,
                         effective_cost: float, account: str = "options"):
        """📌 Put assignment notification."""
        if not self.cfg.alert_assignments:
            return
        self._send(
            f"📌 Put Assigned: {symbol}\n"
            f"Shares: {shares} @ strike ${strike:.2f}\n"
            f"Effective cost: ${effective_cost:.2f}\n"
            f"Account: {account}",
            tags="pushpin",
        )

    def bridge_alert(self, amount: float, currency: str,
                     source: str, target: str, status: str):
        """🌉 Bridge transfer notification."""
        if not self.cfg.alert_bridge:
            return
        emoji = "✅" if status == "completed" else "⚠️"
        self._send(
            f"🌉 Cash Bridge {emoji}\n"
            f"Amount: {currency} {amount:,.2f}\n"
            f"From: {source}\n"
            f"To: {target}\n"
            f"Status: {status}",
            priority="high",
            tags="bridge_at_night",
        )

    def halt_alert(self, reason: str):
        """🔴 Trading halted."""
        self.critical("TRADING HALTED", f"Reason: {reason}\nTime: {datetime.now().strftime('%H:%M:%S')}")

    def resume_alert(self):
        """🟢 Trading resumed."""
        if not self.cfg.alert_critical:
            return
        self._send(
            f"🟢 Trading Resumed\nTime: {datetime.now().strftime('%H:%M:%S')}",
            tags="green_circle",
        )

    def disconnect_alert(self, duration_minutes: int):
        """⚡ Prolonged disconnect."""
        self.critical(
            "IBKR DISCONNECTED",
            f"Connection lost for {duration_minutes} minutes.\n"
            f"Auto-reconnect attempting..."
        )

    def tws_unreachable_alert(self, minutes_down: int):
        """🚨 TWS not responding — needs human intervention."""
        self.critical(
            "TWS UNREACHABLE",
            f"TWS has not responded for {minutes_down} minutes.\n"
            f"Manual login likely required.\n"
            f"All trading is paused until connection is restored."
        )

    def heartbeat_alert(
        self,
        net_liq: float,
        margin_pct: float,
        open_positions: int,
        uptime_hours: float,
        last_scan_ago: str,
        connection_ok: bool,
    ):
        """💚 Daily heartbeat — proof of life."""
        if not self.cfg.alert_daily:
            return
        status = "✅ Connected" if connection_ok else "❌ Disconnected"
        lines = [
            f"💚 Heartbeat — {date.today().strftime('%a %b %d')}",
            f"",
            f"Status:     {status}",
            f"NLV:        €{net_liq:,.0f}",
            f"Margin:     {margin_pct:.1f}%",
            f"Positions:  {open_positions}",
            f"Uptime:     {uptime_hours:.1f}h",
            f"Last scan:  {last_scan_ago}",
        ]
        self._send("\n".join(lines), priority="low", tags="green_heart")

    def margin_warning_alert(self, margin_pct: float, nlv: float):
        """⚠️ Margin approaching danger zone."""
        self.critical(
            "MARGIN WARNING",
            f"Margin usage: {margin_pct:.1f}%\n"
            f"NLV: €{nlv:,.0f}\n"
            f"Approaching maintenance margin — new trades blocked."
        )

    def drawdown_alert(self, drop_pct: float, nlv: float, prev_nlv: float):
        """📉 Large single-day drawdown."""
        self.critical(
            "LARGE DRAWDOWN",
            f"NLV dropped {drop_pct:.1f}% today\n"
            f"Current: €{nlv:,.0f}  (prev: €{prev_nlv:,.0f})"
        )

    def suggestion_alert(self, count: int, symbols: list[str], source: str = "portfolio"):
        """🔔 New suggestions needing manual approval."""
        if not self.cfg.alert_suggestions:
            return
        sym_list = ", ".join(symbols[:8])
        if len(symbols) > 8:
            sym_list += f" +{len(symbols) - 8} more"
        self._send(
            f"🔔 {count} New Suggestions — {source}\n"
            f"Symbols: {sym_list}\n"
            f"Action required: Review & Approve/Reject on dashboard",
            priority="high",
            tags="bell",
        )

    def rescreen_alert(self, stocks_screened: int, sell_suggestions: int,
                       reduce_suggestions: int, cc_suggestions: int):
        """📋 Annual rescreen completed."""
        if not self.cfg.alert_rescreen:
            return
        lines = [
            f"📋 Annual Rescreen Complete",
            f"Screened: {stocks_screened} stocks ($1B+)",
        ]
        if sell_suggestions + reduce_suggestions + cc_suggestions > 0:
            lines.append(f"")
            lines.append(f"⚠️ Review suggestions created:")
            if sell_suggestions > 0:
                lines.append(f"  Sell: {sell_suggestions}")
            if reduce_suggestions > 0:
                lines.append(f"  Reduce: {reduce_suggestions}")
            if cc_suggestions > 0:
                lines.append(f"  Covered calls: {cc_suggestions}")
            lines.append(f"")
            lines.append(f"Manual approval required on dashboard")
        else:
            lines.append(f"✅ All holdings still qualify — no action needed")
        self._send("\n".join(lines), priority="high", tags="clipboard")

    # ── Backend dispatch ─────────────────────────────────────

    def _send(self, message: str, priority: str = "default", tags: str = ""):
        """Send to all enabled backends."""
        if not self.cfg.enabled:
            return

        # Always log
        log.info("alert_sent", message=message[:100])

        # Telegram
        if self.cfg.telegram_enabled and self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            try:
                self._send_telegram(message)
            except Exception as e:
                log.warning("telegram_alert_failed", error=str(e))

        # ntfy.sh
        if self.cfg.ntfy_enabled and self.cfg.ntfy_topic:
            try:
                self._send_ntfy(message, priority, tags)
            except Exception as e:
                log.warning("ntfy_alert_failed", error=str(e))

    def _send_telegram(self, message: str):
        """Send via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Telegram API returned {resp.status}")

    def _send_ntfy(self, message: str, priority: str = "default", tags: str = ""):
        """Send via ntfy.sh (or self-hosted ntfy)."""
        url = f"{self.cfg.ntfy_server}/{self.cfg.ntfy_topic}"

        headers = {
            "Content-Type": "text/plain",
        }
        if priority and priority != "default":
            # ntfy priorities: min, low, default, high, urgent
            headers["Priority"] = priority
        if tags:
            headers["Tags"] = tags

        req = urllib.request.Request(
            url,
            data=message.encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ntfy returned {resp.status}")


# ── Singleton ────────────────────────────────────────────────
_alert_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    """Get or create the singleton AlertManager."""
    global _alert_manager
    if _alert_manager is None:
        # Load config from settings
        _alert_manager = AlertManager(AlertConfig())
    return _alert_manager


def init_alerts(cfg: AlertConfig) -> AlertManager:
    """Initialize alerts with specific config."""
    global _alert_manager
    _alert_manager = AlertManager(cfg)
    return _alert_manager
