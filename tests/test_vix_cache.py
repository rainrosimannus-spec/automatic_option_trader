"""
get_vix(): one read per scan, not one per symbol. 2026-10-08, with IBKR's history farm broken,
a 60-name scan made ~230 VIX history requests at an 8 s timeout each before falling back to
FMP — ~30 min of one scan. Now: 2-min cache, and after an IBKR miss a 10-min backoff that goes
straight to FMP.
"""
from datetime import datetime, timedelta

import src.broker.market_data as md


class _Calls:
    def __init__(self, ibkr=None, fmp=None):
        self.ibkr_value, self.fmp_value = ibkr, fmp
        self.ibkr = 0
        self.fmp = 0

    def from_ibkr(self):
        self.ibkr += 1
        return self.ibkr_value

    def from_fmp(self):
        self.fmp += 1
        return self.fmp_value


def _wire(monkeypatch, calls):
    md._reset_vix_cache()
    monkeypatch.setattr(md, "_get_vix_from_ibkr", calls.from_ibkr)
    monkeypatch.setattr(md, "_get_vix_from_fmp", calls.from_fmp)


def test_cached_within_ttl_one_fetch_for_many_symbols(monkeypatch):
    c = _Calls(ibkr=15.6)
    _wire(monkeypatch, c)
    assert [md.get_vix() for _ in range(60)] == [15.6] * 60
    assert c.ibkr == 1 and c.fmp == 0


def test_ibkr_miss_falls_back_to_fmp_then_backs_off_ibkr(monkeypatch):
    c = _Calls(ibkr=None, fmp=15.08)
    _wire(monkeypatch, c)
    assert md.get_vix() == 15.08
    assert c.ibkr == 1 and c.fmp == 1
    # cache expired but backoff still active → FMP only, IBKR not retried
    md._vix_cache = (15.08, datetime.now() - timedelta(seconds=md._VIX_CACHE_TTL_SEC + 1))
    assert md.get_vix() == 15.08
    assert c.ibkr == 1 and c.fmp == 2
    # backoff over → IBKR tried again
    md._vix_ibkr_backoff_until = datetime.now() - timedelta(seconds=1)
    md._vix_cache = None
    md.get_vix()
    assert c.ibkr == 2


def test_force_refresh_bypasses_cache_and_success_clears_backoff(monkeypatch):
    c = _Calls(ibkr=16.2)
    _wire(monkeypatch, c)
    md._vix_cache = (15.0, datetime.now())
    md._vix_ibkr_backoff_until = None
    assert md.get_vix(force_refresh=True) == 16.2
    assert c.ibkr == 1
    assert md._vix_ibkr_backoff_until is None


def test_total_miss_returns_none_and_caches_nothing(monkeypatch):
    c = _Calls(ibkr=None, fmp=None)
    _wire(monkeypatch, c)
    assert md.get_vix() is None
    assert md._vix_cache is None
