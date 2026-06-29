"""SEC-EDGAR IPO ticker + first-trading-day resolver (replaces the Finnhub/scrape guesses).

Network calls (_fts / _ticker_from_doc) are stubbed so these test the pure logic: display-name
parsing, loose name-matching, EARLIEST-final-prospectus selection (so a later follow-on 424B4 can't
set the IPO date), and the S-1 pre-pricing fallback (proposed ticker, no firm date).
"""
import src.ipo.listing as L


def _hit(names, file_date, file_type="424B4", _id="0001-24-1:doc.htm"):
    return {"_source": {"display_names": [names], "file_date": file_date, "file_type": file_type},
            "_id": _id}


def test_display_name_parsing():
    assert L._DISPLAY.search("Reddit, Inc.  (RDDT)  (CIK 0001713445)").groups() == ("RDDT", "0001713445")


def test_name_match_ignores_suffixes():
    assert L._name_matches("Reddit", "Reddit, Inc.  (RDDT)  (CIK 0001713445)")
    assert L._name_matches("Circle Internet", "Circle Internet Group, Inc. (CRCL) (CIK 0001876042)")
    assert not L._name_matches("Stripe", "Reddit, Inc.  (RDDT)  (CIK 0001713445)")


def test_symbol_from_prospectus_text():
    assert L._SYMBOL.search('to list our common stock under the symbol "ABCD" on').group(1) == "ABCD"
    assert L._SYMBOL.search("under the symbol “WXYZ” on Nasdaq").group(1) == "WXYZ"


def test_resolve_picks_earliest_final_prospectus(monkeypatch):
    # A later follow-on 424B4 must NOT set the IPO date — the earliest one wins.
    monkeypatch.setattr(L, "_fts", lambda name, forms: [
        _hit("Tempus AI, Inc. (TEM) (CIK 0001717115)", "2025-04-23"),   # follow-on
        _hit("Tempus AI, Inc. (TEM) (CIK 0001717115)", "2024-06-17"),   # the IPO
    ])
    r = L.resolve_listing("Tempus AI")
    assert r["ticker"] == "TEM"
    assert r["first_trading_day"] == "2024-06-17"
    assert r["confidence"] == "confirmed"


def test_resolve_falls_back_to_s1_proposed_ticker(monkeypatch):
    # No final prospectus yet (pre-pricing) → read proposed ticker from the S-1, no firm date.
    def fake_fts(name, forms):
        if forms == L._FINAL_FORMS:
            return []
        return [_hit("NewCo, Inc. (CIK 0001999999)", "2026-05-01", file_type="S-1")]
    monkeypatch.setattr(L, "_fts", fake_fts)
    monkeypatch.setattr(L, "_ticker_from_doc", lambda cik, doc: ("NEWC", "Nasdaq"))
    r = L.resolve_listing("NewCo")
    assert r["ticker"] == "NEWC"
    assert r["first_trading_day"] is None
    assert r["confidence"] == "low"
    assert r["source"] == "S-1"


def test_resolve_none_when_no_match(monkeypatch):
    monkeypatch.setattr(L, "_fts", lambda name, forms: [
        _hit("Unrelated Corp (CIK 0001234567)", "2024-01-01")])
    assert L.resolve_listing("Reddit") is None
