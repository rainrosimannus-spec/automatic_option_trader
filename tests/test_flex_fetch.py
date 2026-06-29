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
