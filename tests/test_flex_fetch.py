"""IBKR Flex fetch resilience (2026-06-29): the deposit sync was dying on IBKR's transient
"Statement could not be generated at this time. Please try again shortly." (and the 'generation
in progress' ErrorCode 1019), which are RETRYABLE — not fatal. `_flex_is_transient` decides
which Flex errors should be retried vs raised, so deposits actually land in the invested ledger.
"""
from src.portfolio.capital_injections import _flex_is_transient


def test_send_request_throttle_message_is_transient():
    assert _flex_is_transient(
        None, "Statement could not be generated at this time. Please try again shortly."
    ) is True


def test_error_code_1019_is_transient():
    assert _flex_is_transient("1019", "Statement generation in progress") is True


def test_generation_in_progress_text_is_transient():
    assert _flex_is_transient(None, "Statement generation in progress.") is True


def test_permanent_error_is_not_transient():
    # A real config/permission error must NOT be retried — it should raise.
    assert _flex_is_transient("1003", "Invalid query.") is False
    assert _flex_is_transient(None, "Token has expired.") is False


def test_empty_is_not_transient():
    assert _flex_is_transient(None, None) is False
    assert _flex_is_transient("", "") is False


class _Resp:
    def __init__(self, text):
        self.text = text
    def raise_for_status(self):
        pass


def test_getstatement_in_progress_envelope_is_not_returned_as_data(monkeypatch):
    """The finished statement is <FlexQueryResponse>. A <FlexStatementResponse> envelope during the
    GetStatement poll (ErrorCode 1019 'generation in progress' — what the slower options query
    returned) must be RETRIED, not returned as the statement (which silently parsed to 0 deposits)."""
    import src.portfolio.capital_injections as ci

    send = '<FlexStatementResponse><Status>Success</Status><ReferenceCode>123</ReferenceCode></FlexStatementResponse>'
    in_progress = ('<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>'
                   '<ErrorMessage>Statement generation in progress, please try again shortly.</ErrorMessage></FlexStatementResponse>')
    real = '<FlexQueryResponse queryName="x"><FlexStatements><FlexStatement/></FlexStatements></FlexQueryResponse>'
    seq = [_Resp(send), _Resp(in_progress), _Resp(real)]

    monkeypatch.setattr(ci.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ci.requests, "get", lambda *a, **k: seq.pop(0))

    out = ci.fetch_flex_statement("tok", "qid")
    assert "<FlexQueryResponse" in out          # polled PAST the 1019 envelope to the real statement
    assert "1019" not in out
    assert seq == []                             # consumed send + in-progress + real
