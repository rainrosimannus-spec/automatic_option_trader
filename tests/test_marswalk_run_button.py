"""2026-11-26 (Rain): "I pushed the marswalk button, I cannot see anything happening."

The server side was fine (a replayed POST started the sweep at once). The browser never sent it:
the number inputs carried step="0.05" / "50000" and two defaults that changed on 11-24/25 — the
calibrated uplift 0.33 and the real deposit 251,000 — are not multiples of their step, so HTML5
validation refused the submit and showed only a bubble on the field. Also: progress was a whole-page
refresh every 8 s, and a pressed button left no log line (no access log on the web server).
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = (ROOT / "src/web/templates/marswalk.html").read_text()
R = (ROOT / "src/web/routes/marswalk.py").read_text()

FULL_FORM = dict(dte_min=0, dte_max=3, delta_min=0.2, delta_max=0.3, put_min_premium=0.5,
                 cc_dte_min=1, cc_dte_max=7, cc_delta_min=0.35, cc_delta_max=0.45, cc_min_premium=0.2,
                 start_nlv=251_000.0, collateral_cap_pct=30.0, uplift_k=0.33, gap_stress_pct=0,
                 margin_on="1", margin_multiple=5.0, max_positions=15, iv_rank_min=20.0, vix_halt=35.0,
                 max_margin_usage_pct=80.0)


def test_number_inputs_never_block_the_submit_on_step():
    # the two live values that killed the button are not multiples of the old steps
    assert round((0.33 / 0.05) % 1, 6) not in (0.0, 1.0) and 251_000 % 50_000 != 0
    assert 'step="any" data-step="{{ step }}"' in T          # validation off, +/- buttons keep their step
    assert "el.dataset.step" in T
    assert 'http-equiv="refresh"' not in T                   # progress is polled, not a page refresh
    assert "reportValidity()" in T and 'id="mw-invalid"' in T  # an out-of-range value is said out loud


def test_every_form_default_lies_inside_its_browser_min_max(monkeypatch):
    from src.web.routes import marswalk as mw
    monkeypatch.setattr(mw, "_default_nlv", lambda fallback=220_000.0: 251_000.0)
    d = mw._form_defaults()
    calls = re.findall(r"num\('(\w+)', defaults\.(\w+), ([\d.]+), ([\d.]+), ([\d.]+)", T)
    assert len(calls) >= 18
    for name, key, _step, mn, mx in calls:
        v = float(d[key])
        assert float(mn) <= v <= float(mx), (name, v, mn, mx)   # min/max are still enforced by the browser


def test_status_endpoint_reports_the_sweep_state(monkeypatch):
    from src.web.routes import marswalk as mw
    from src.marswalk import service
    monkeypatch.setattr(service, "_state", {"active": True, "msg": "running q4_2018", "done": 3,
                                            "total": 22, "queued": False, "finished_at": None})
    body = json.loads(mw.marswalk_status().body)
    assert body["active"] is True and body["done"] == 3 and body["msg"] == "running q4_2018"
    assert "fetch('/marswalk/status'" in T and "location.reload()" in T


def test_run_now_logs_the_press_and_launches_the_sweep(monkeypatch):
    from src.web.routes import marswalk as mw
    from src.marswalk import service
    launched, logged = [], []
    monkeypatch.setattr(service, "run_all_async", lambda p, fetch=True: launched.append((p, fetch)))
    monkeypatch.setattr(mw.log, "info", lambda ev, **kw: logged.append((ev, kw)))
    resp = mw.marswalk_run(**FULL_FORM)
    assert resp.status_code == 303 and resp.headers["location"] == "/marswalk"
    assert len(launched) == 1 and isinstance(launched[0][1], bool)
    assert [e for e, _ in logged] == ["marswalk_run_requested"] and logged[0][1]["uplift_k"] == 0.33
