"""Permission-block registry (2026-06-29): names on an exchange the account can't trade yet
(IBKR Error 200) are paused so the compounder scan stops creating doomed suggestions for them
and the daily deploy budget flows to fillable names. Auto-expires; stays ranked/visible meanwhile.
"""
from types import SimpleNamespace

from src.portfolio.buyer import (
    _mark_permission_blocked, _is_permission_blocked, _order_blocked_by_permission,
)


def _trade(*entries):
    """entries: (errorCode, message) tuples → a fake ib_insync Trade with a .log."""
    return SimpleNamespace(log=[SimpleNamespace(errorCode=c, message=m) for c, m in entries])


def test_error_200_is_permission_block():
    assert _order_blocked_by_permission(
        _trade((0, "PendingSubmit"), (200, "No security definition has been found for the request"))
    ) is True


def test_permission_keyword_in_message():
    assert _order_blocked_by_permission(_trade((0, "No trading permission for this exchange"))) is True


def test_transient_cancel_is_not_permission_block():
    assert _order_blocked_by_permission(_trade((0, "PendingSubmit"), (202, "Order Canceled"))) is False


def test_empty_log_is_not_block():
    assert _order_blocked_by_permission(_trade()) is False
    assert _order_blocked_by_permission(SimpleNamespace(log=None)) is False


def test_mark_and_check_block():
    _mark_permission_blocked("3690")
    assert _is_permission_blocked("3690") is True
    assert _is_permission_blocked("AVGO") is False     # unrelated symbol not blocked


def test_block_expires():
    _mark_permission_blocked("STALE", hours=-1)         # already in the past
    assert _is_permission_blocked("STALE") is False
