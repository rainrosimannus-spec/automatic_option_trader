"""Which stored `source` values each Suggestions page shows.

The monthly screener/review writes its cards with source="rescreen" and the portfolio page used to
match the literal string "portfolio" only — so every review card ever produced (sell_stock_review,
reduce_position_review, sell_covered_call_review) was invisible there, with no way to accept or
reject it (found 2026-09-09). The options page must NOT pick them up."""
from src.web.routes.suggestions import _PAGE_SOURCES


def test_portfolio_page_owns_rescreen_cards():
    assert "rescreen" in _PAGE_SOURCES["portfolio"]
    assert "portfolio" in _PAGE_SOURCES["portfolio"]


def test_options_page_does_not():
    assert "rescreen" not in _PAGE_SOURCES["options"]
    assert "portfolio" not in _PAGE_SOURCES["options"]


def test_pending_filter_uses_the_page_map(monkeypatch):
    from types import SimpleNamespace
    import src.web.routes.suggestions as mod

    rows = [SimpleNamespace(source="rescreen", status="pending", id=1),
            SimpleNamespace(source="portfolio", status="pending", id=2),
            SimpleNamespace(source="options", status="pending", id=3)]
    monkeypatch.setattr(mod, "get_pending_suggestions", lambda: rows)

    class _Q:
        def __init__(self, *a): pass
        def filter(self, *a): return self
        def order_by(self, *a): return self
        def limit(self, *a): return self
        def all(self): return []

    class _DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def query(self, *a): return _Q()

    monkeypatch.setattr(mod, "get_db", lambda: _DB())
    pending, _ = mod._get_suggestions_by_source("portfolio")
    assert sorted(s.id for s in pending) == [1, 2]
    pending, _ = mod._get_suggestions_by_source("options")
    assert [s.id for s in pending] == [3]
