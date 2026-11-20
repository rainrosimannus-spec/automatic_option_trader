"""2026-11-20: the options gateway's daily 07:00-Lux auto-restart must not read as an outage.

(1) Dashboard header says "reconnecting — daily gateway restart" (amber) instead of "Offline" (red)
    while the restart window is open; (2) the health check re-checks every minute while down and
    arms an early retry from the socket-drop event; (3) the TWS-unreachable alert is keyed to
    minutes down, not to the number of (now more frequent) checks.
"""
from datetime import datetime, time as dtime
from pathlib import Path
from unittest.mock import patch

import pytz

from src.broker import gateway_schedule as gs

LUX = pytz.timezone("Europe/Luxembourg")
HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "dashboard.html"


# ── ini parsing ────────────────────────────────────────────────────────────────────────────
def test_parse_ibc_formats():
    assert gs.parse_auto_restart_time("AutoRestartTime=07:00 AM") == dtime(7, 0)
    assert gs.parse_auto_restart_time("AutoRestartTime=02:00 AM\r\nColdRestartTime=11:00 AM\r\n") == dtime(2, 0)
    assert gs.parse_auto_restart_time("AutoRestartTime=11:30 PM") == dtime(23, 30)
    assert gs.parse_auto_restart_time("AutoRestartTime=12:15 AM") == dtime(0, 15)
    assert gs.parse_auto_restart_time("AutoRestartTime=19:45") == dtime(19, 45)
    assert gs.parse_auto_restart_time("# AutoRestartTime=07:00 AM\nOther=1") is None
    assert gs.parse_auto_restart_time("AutoRestartTime=") is None


def test_read_from_crlf_file_and_fallbacks(tmp_path):
    ini = tmp_path / "config-options.ini"
    ini.write_bytes(b"[IBC]\r\nColdRestartTime=11:00 AM\r\nAutoRestartTime=07:00 AM\r\n")
    assert gs.read_auto_restart_time(ini) == dtime(7, 0)
    ini.write_bytes(b"AutoRestartTime=garbage\r\n")
    assert gs.read_auto_restart_time(ini) == gs.DEFAULT_RESTART      # malformed → default
    assert gs.read_auto_restart_time(tmp_path / "missing.ini") == gs.DEFAULT_RESTART


# ── window ─────────────────────────────────────────────────────────────────────────────────
def _lux(h, m, s=0):
    return LUX.localize(datetime(2026, 11, 20, h, m, s))


def test_window_boundaries():
    r = dtime(7, 0)
    assert not gs.restart_window_active(_lux(6, 59, 59), r)
    assert gs.restart_window_active(_lux(7, 0, 0), r)
    assert gs.restart_window_active(_lux(7, 9, 59), r)
    assert not gs.restart_window_active(_lux(7, 10, 0), r)
    assert not gs.restart_window_active(_lux(19, 5), r)


def test_gateway_restart_window_dict(tmp_path):
    ini = tmp_path / "ini"; ini.write_text("AutoRestartTime=07:00 AM\n")
    inside = _lux(7, 4).astimezone(pytz.UTC)      # 06:04 UTC = the 2026-11-20 incident minute
    assert gs.gateway_restart_window(inside, ini) == {"start": "07:00", "until": "07:10", "tz": "Luxembourg"}
    assert gs.gateway_restart_window(_lux(7, 12).astimezone(pytz.UTC), ini) is None
    assert gs.gateway_restart_window(_lux(2, 3).astimezone(pytz.UTC), ini) is None   # old 02:00 slot: nothing


def test_template_has_reconnecting_chip():
    t = HTML.read_text()
    assert "daily gateway restart" in t
    assert "{% if not connected and gateway_restart %}" in t
    assert "IBKR {{ 'Online' if connected else 'Offline' }}" in t     # normal chip still there


# ── health check: minute retries + elapsed-time alert ─────────────────────────────────────
class _FakeSched:
    running = True

    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kw):
        self.jobs.append((func, trigger, kw))


def _reset(jobs):
    jobs._consecutive_disconnect_checks = 0
    jobs._tws_unreachable_alerted = False
    jobs._disconnect_since = None


def test_health_check_arms_minute_retry_and_alerts_on_elapsed_time():
    from src.scheduler import jobs
    _reset(jobs)
    sched = _FakeSched()
    alerts = []

    class _Alerts:
        def tws_unreachable_alert(self, minutes_down):
            alerts.append(minutes_down)

    with patch.object(jobs, "_ensure_event_loop"), \
         patch.object(jobs, "is_connected", return_value=False), \
         patch.object(jobs, "_ensure_connected", return_value=False), \
         patch.object(jobs, "get_scheduler", return_value=sched), \
         patch("src.core.alerts.get_alert_manager", return_value=_Alerts()):
        jobs.job_health_check()
        assert len(sched.jobs) == 1
        func, trig, kw = sched.jobs[0]
        assert func is jobs.job_health_check and kw["id"] == "health_check_retry" and kw["replace_existing"]
        assert alerts == []                                  # minute 0: no alert
        assert jobs._disconnect_since is not None

        # three more checks within the first minutes: still no alert (old code alerted at check #3)
        for _ in range(3):
            jobs.job_health_check()
        assert alerts == []

        # 16 minutes down → one alert, then no repeat
        jobs._disconnect_since -= 16 * 60
        jobs.job_health_check()
        jobs.job_health_check()
        assert len(alerts) == 1 and alerts[0] >= 15

    # reconnect clears everything
    with patch.object(jobs, "_ensure_event_loop"), \
         patch.object(jobs, "is_connected", return_value=True), \
         patch.object(jobs, "_detect_stale_connection"), \
         patch("src.broker.orders.refresh_open_orders_cache"):
        jobs.job_health_check()
    assert jobs._disconnect_since is None and jobs._consecutive_disconnect_checks == 0
    assert jobs._tws_unreachable_alerted is False
    _reset(jobs)


def test_disconnect_event_arms_early_retry_and_is_noop_without_scheduler():
    from src.scheduler import jobs
    sched = _FakeSched()
    with patch.object(jobs, "get_scheduler", return_value=sched):
        jobs.on_options_disconnected()
    assert len(sched.jobs) == 1 and sched.jobs[0][2]["id"] == "health_check_retry"
    run_at = sched.jobs[0][1].run_date
    assert 20 <= (run_at - datetime.now(pytz.UTC)).total_seconds() <= 31

    with patch.object(jobs, "get_scheduler", return_value=None):
        jobs.on_options_disconnected()                      # no scheduler → nothing, no raise
    stopped = _FakeSched(); stopped.running = False
    with patch.object(jobs, "get_scheduler", return_value=stopped):
        jobs.on_options_disconnected()
    assert stopped.jobs == []


def test_connection_wires_disconnect_event():
    src = Path(__file__).resolve().parents[1] / "src" / "broker" / "connection.py"
    t = src.read_text()
    assert "ib.disconnectedEvent += _on_disconnected" in t
    assert "from src.scheduler.jobs import on_options_disconnected" in t
