"""Call B of the breakthrough selection fails (API credit exhausted, HTTP error, bad JSON):
the tier keeps the previous selection instead of emptying. 2026-11-03 the empty tier dropped the
screen to 75 names and the collapse guard aborted the whole run."""
import tools.screen_universe as su


class _Session:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


FRESH = [{"symbol": "NEWA", "exchange": "SMART", "currency": "USD", "name": "New A",
          "megatrend": "x", "thesis": "t"},
         {"symbol": "NEWB", "exchange": "SMART", "currency": "USD", "name": "New B",
          "megatrend": "x", "thesis": "t"}]
ANCHOR = [{"symbol": "OLD1", "exchange": "SMART", "currency": "USD", "name": "Old 1"},
          {"symbol": "OLD2", "exchange": "TSEJ", "currency": "JPY", "name": "Old 2"},
          {"symbol": "NEWA", "exchange": "SMART", "currency": "USD", "name": "New A"}]


def _run(monkeypatch, response, fresh, anchor):
    monkeypatch.setattr(su, "_call_claude_for_selection", lambda prompt: response)
    sess = _Session()
    out = su._run_breakthrough_selection(fresh=fresh, anchor=anchor, audit_session=sess,
                                         run_date=None)
    return out, sess


def test_call_b_failure_keeps_previous_selection(monkeypatch):
    out, sess = _run(monkeypatch, {}, FRESH, ANCHOR)
    assert out["fallback_used"] is True
    assert out["selected_symbols"] == {"OLD1", "OLD2", "NEWA"}
    assert "NEWB" not in out["selected_symbols"]          # no entry without the judgement call
    assert out["notes"] == "call_B_failed_empty_response:anchor_kept"
    assert all("previous selection kept" in i["reasoning"] for i in out["selected_with_reasoning"])
    assert sess.rows and sess.rows[0].fallback_used is True


def test_call_b_failure_with_fresh_but_no_anchor_scan_failed(monkeypatch):
    # Scan (call A) failed too: nothing fresh, anchor present -> the tier is the anchor.
    out, _ = _run(monkeypatch, {}, [], ANCHOR)
    assert out["selected_symbols"] == {"OLD1", "OLD2", "NEWA"}


def test_call_b_failure_bootstrap_takes_fresh(monkeypatch):
    out, _ = _run(monkeypatch, {}, FRESH, [])
    assert out["selected_symbols"] == {"NEWA", "NEWB"}
    assert out["notes"] == "call_B_failed_empty_response:fresh_as_is"


def test_call_b_no_valid_picks_keeps_previous_selection(monkeypatch):
    out, _ = _run(monkeypatch, {"selected": [{"nope": 1}], "group_reasoning": ""}, FRESH, ANCHOR)
    assert out["fallback_used"] is True
    assert out["selected_symbols"] == {"OLD1", "OLD2", "NEWA"}
    assert out["notes"] == "call_B_returned_no_valid_picks:anchor_kept"


def test_call_b_success_unchanged(monkeypatch):
    resp = {"selected": [{"symbol": "OLD1", "reasoning": "r"}, {"symbol": "NEWB", "reasoning": "r"}],
            "group_reasoning": "g"}
    out, _ = _run(monkeypatch, resp, FRESH, ANCHOR)
    assert out["fallback_used"] is False
    assert out["selected_symbols"] == {"OLD1", "NEWB"}
    assert out["notes"] == "ok"
