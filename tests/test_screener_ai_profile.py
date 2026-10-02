"""Screener AI helper: model profiles, web search, and reading the answer out of the stream.

The default profile must keep sending exactly what was approved (Opus 4.8, no tools); the
candidate profile (Opus 5.5 + web search) has thinking always on and interleaves search blocks,
so the answer has to be read by block type, not position.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
su = pytest.importorskip("screen_universe")
import src.core.config as config_mod


class _Resp:
    def __init__(self, events, status=200, text=""):
        self._events, self.status_code, self.text = events, status, text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self, decode_unicode=True):
        for e in self._events:
            yield "event: " + e["type"]
            yield "data: " + json.dumps(e)


def _start(i, btype):
    return {"type": "content_block_start", "index": i, "content_block": {"type": btype}}


def _text(i, s):
    return {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": s}}


def _think(i, s):
    return {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": s}}


def _end(stop="end_turn"):
    return [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"}]


@pytest.fixture
def api(monkeypatch):
    sent, queue = [], []
    monkeypatch.setattr(config_mod, "get_settings",
                        lambda: SimpleNamespace(raw={"anthropic": {"api_key": "k"}}))

    def fake_post(url, headers=None, json=None, stream=None, timeout=None):
        sent.append({"headers": headers, "payload": json, "timeout": timeout})
        return queue.pop(0)
    monkeypatch.setattr(su.requests, "post", fake_post)
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    return SimpleNamespace(sent=sent, queue=queue)


def test_default_profile_is_the_approved_one():
    assert su.SCREENER_AI_PROFILE in su.SCREENER_AI_PROFILES
    # Flipping this line is a SELECTION change — run scripts/screener_ai_side_by_side.py and get
    # Rain's go first, then update this assertion in the same commit.
    # 2026-10-02: switched from "opus-4-8" on Rain's go after the side-by-side.
    assert su.SCREENER_AI_PROFILE == "opus-5-5-search"
    assert su.SCREENER_AI_FALLBACK_PROFILE == "opus-4-8"


def test_legacy_profile_sends_no_tools_effort_or_beta(api):
    api.queue.append(_Resp([{"type": "message_start", "message": {"model": "claude-opus-4-8"}},
                            _start(0, "text"), _text(0, "[1]"), *_end()]))
    out = su._anthropic_messages("p", 4000, "t", web_search=True, profile="opus-4-8")
    p = api.sent[0]["payload"]
    assert out == "[1]" and p["model"] == "claude-opus-4-8" and p["max_tokens"] == 4000
    assert "tools" not in p and "output_config" not in p and "fallbacks" not in p
    assert "anthropic-beta" not in api.sent[0]["headers"]
    assert p["messages"][0]["content"] == "p"          # prompt untouched — no search hint


def test_search_profile_request_shape(api):
    api.queue.append(_Resp([_start(0, "text"), _text(0, "[]"), *_end()]))
    su._anthropic_messages("p", 4000, "t", web_search=True, profile="opus-5-5-search")
    p, h = api.sent[0]["payload"], api.sent[0]["headers"]
    assert p["model"] == "claude-opus-5-5"
    assert p["max_tokens"] >= 64000                    # thinking counts against the cap
    assert p["output_config"] == {"effort": "high"}    # default on this model is medium
    assert "thinking" not in p                         # always on; disabling it is a 400
    assert p["tools"][0]["type"] == "web_search_20260209" and p["tools"][0]["name"] == "web_search"
    assert p["fallbacks"] == "default" and h["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert "WEB SEARCH" in p["messages"][0]["content"]


def test_search_profile_without_search_flag_has_no_tools(api):
    api.queue.append(_Resp([_start(0, "text"), _text(0, "{}"), *_end()]))
    su._anthropic_messages("p", 8000, "selection", profile="opus-5-5-search")
    p = api.sent[0]["payload"]
    assert "tools" not in p and p["messages"][0]["content"] == "p"


def test_answer_is_read_by_block_type_and_narration_before_a_search_is_dropped(api):
    api.queue.append(_Resp([
        {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 9}}},
        _start(0, "thinking"), _think(0, "hmm"),
        _start(1, "text"), _text(1, "Let me look that up."),
        _start(2, "server_tool_use"),
        _start(3, "web_search_tool_result"),
        _start(4, "thinking"), _think(4, "now answer"),
        _start(5, "text"), _text(5, '[{"symbol": '), _text(5, '"SPCX"}]'),
        *_end()]))
    out = su._anthropic_messages("p", 4000, "scan", web_search=True, profile="opus-5-5-search")
    assert out == '[{"symbol": "SPCX"}]'
    assert su.LAST_AI_CALL["searches"] == 1 and su.LAST_AI_CALL["model"] == "claude-opus-5-5"


def test_refusal_raises_instead_of_returning_an_empty_answer(api):
    api.queue.append(_Resp([_start(0, "text"), *_end("refusal")]))
    with pytest.raises(RuntimeError, match="declined"):
        su._anthropic_messages("p", 4000, "scan", profile="opus-5-5-search")


def test_unfinished_search_call_reruns_once_without_search(api):
    api.queue.append(_Resp([_start(0, "server_tool_use"), *_end("pause_turn")]))
    api.queue.append(_Resp([_start(0, "text"), _text(0, "[2]"), *_end()]))
    out = su._anthropic_messages("p", 4000, "scan", web_search=True, profile="opus-5-5-search")
    assert out == "[2]" and len(api.sent) == 2
    assert "tools" in api.sent[0]["payload"] and "tools" not in api.sent[1]["payload"]


def test_http_error_carries_the_api_message(api):
    api.queue.append(_Resp([], status=400, text='{"error":{"message":"bad field"}}'))
    with pytest.raises(su.requests.HTTPError, match="bad field"):
        su._anthropic_messages("p", 4000, "scan", profile="opus-5-5-search")


def test_only_the_discovery_calls_ask_for_search():
    src = Path(su.__file__).read_text()
    assert src.count("web_search=True,  #") == 2      # breakthrough scan + swap proposals


# ── safety net: the active profile failing must not fail the screen ──────────

def test_api_rejection_on_the_active_profile_falls_back_to_the_approved_one(api):
    api.queue.append(_Resp([], status=400, text='{"error":{"message":"unknown field fallbacks"}}'))
    api.queue.append(_Resp([_start(0, "text"), _text(0, "[3]"), *_end()]))
    out = su._anthropic_messages("p", 4000, "scan", web_search=True)
    assert out == "[3]"
    assert api.sent[0]["payload"]["model"] == "claude-opus-5-5"
    assert api.sent[1]["payload"]["model"] == "claude-opus-4-8" and "tools" not in api.sent[1]["payload"]


def test_refusal_on_the_active_profile_falls_back(api):
    api.queue.append(_Resp([_start(0, "text"), *_end("refusal")]))
    api.queue.append(_Resp([_start(0, "text"), _text(0, "{}"), *_end()]))
    assert su._anthropic_messages("p", 8000, "selection") == "{}"
    assert api.sent[1]["payload"]["model"] == "claude-opus-4-8"


def test_explicit_profile_never_falls_back(api):
    api.queue.append(_Resp([], status=400, text="nope"))
    with pytest.raises(su.requests.HTTPError):
        su._anthropic_messages("p", 4000, "scan", profile="opus-5-5-search")
    assert len(api.sent) == 1


def test_fallback_profile_failure_still_raises(api, monkeypatch):
    monkeypatch.setattr(su, "SCREENER_AI_PROFILE", "opus-4-8")
    api.queue.append(_Resp([], status=500, text="down"))
    with pytest.raises(su.requests.HTTPError):
        su._anthropic_messages("p", 4000, "scan")
    assert len(api.sent) == 1
