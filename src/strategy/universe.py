"""
Stock universe manager — loads and manages the 50-stock watchlist.
"""
from __future__ import annotations

from src.core.config import get_options_universe, StockEntry
from src.core.logger import get_logger

log = get_logger(__name__)


class UniverseManager:
    """Manages the global stock universe across multiple markets."""

    # Market session definitions: exchange code → (timezone, open_hour, open_min, close_hour, close_min)
    MARKET_SESSIONS = {
        # North America — includes pre-market (4 AM) and after-hours (to 20:00)
        "SMART": ("US/Eastern", 4, 0, 20, 0),
        # Virtual European session — EUR/CHF/GBP/NOK/SEK/DKK stocks
        # European exchanges 8:00-22:00 CET covers pre/post market
        "SMART_EU": ("Europe/Berlin", 8, 0, 22, 0),
        # Virtual Asian session — JPY/AUD/HKD etc stocks
        "SMART_ASIA": ("Asia/Tokyo", 9, 0, 16, 0),        # Covers Tokyo + ASX overlap
        # Note: Canadian TSE shares code with Japan TSE — handled by currency
        # Europe
        "LSE":   ("Europe/London", 8, 0, 16, 30),          # London Stock Exchange
        "IBIS":  ("Europe/Berlin", 9, 0, 17, 30),          # Xetra / Frankfurt
        "SBF":   ("Europe/Paris", 9, 0, 17, 30),           # Euronext Paris
        "AEB":   ("Europe/Amsterdam", 9, 0, 17, 30),       # Euronext Amsterdam
        "SWX":   ("Europe/Zurich", 9, 0, 17, 30),          # SIX Swiss Exchange
        "BM":    ("Europe/Madrid", 9, 0, 17, 30),          # Bolsa de Madrid
        "BVME":  ("Europe/Rome", 9, 0, 17, 30),            # Borsa Italiana
        "SFB":   ("Europe/Stockholm", 9, 0, 17, 30),       # Nasdaq Stockholm
        "CSE":   ("Europe/Copenhagen", 9, 0, 17, 0),       # Nasdaq Copenhagen
        "HEX":   ("Europe/Helsinki", 10, 0, 18, 30),       # Nasdaq Helsinki
        "OSE":   ("Europe/Oslo", 9, 0, 16, 20),            # Oslo / Euronext
        "ENEXT.BE": ("Europe/Brussels", 9, 0, 17, 30),     # Euronext Brussels
        "ISE":   ("Europe/Dublin", 8, 0, 16, 30),          # Euronext Dublin
        # Asia-Pacific
        "TSE":   ("Asia/Tokyo", 9, 0, 15, 0),              # Tokyo Stock Exchange
        "TSEJ":  ("Asia/Tokyo", 9, 0, 15, 0),              # Tokyo (IBKR alias)
        "SEHK":  ("Asia/Hong_Kong", 9, 30, 16, 0),         # Hong Kong
        "SGX":   ("Asia/Singapore", 9, 0, 17, 0),          # Singapore
        "ASX":   ("Australia/Sydney", 10, 0, 16, 0),       # Australian Securities Exchange
        "KSE":   ("Asia/Seoul", 9, 0, 15, 30),             # Korea Exchange
        "NSE":   ("Asia/Kolkata", 9, 15, 15, 30),          # National Stock Exchange India
        "IDX":   ("Asia/Jakarta", 9, 0, 16, 0),            # Indonesia Stock Exchange
        "TWSE":  ("Asia/Taipei", 9, 0, 13, 30),            # Taiwan Stock Exchange
        # Middle East & Africa
        "TASE":  ("Asia/Jerusalem", 10, 0, 17, 30),        # Tel Aviv Stock Exchange
        "JSE":   ("Africa/Johannesburg", 9, 0, 17, 0),     # Johannesburg Stock Exchange
        # Latin America
        "BVMF":  ("America/Sao_Paulo", 10, 0, 17, 0),     # B3 / Bovespa
        "MEXI":  ("America/Mexico_City", 8, 30, 15, 0),    # Mexican Stock Exchange
    }

    def __init__(self):
        # Options trader reads from options_universe.yaml (top 50 by options_score)
        # Falls back to watchlist.yaml until first monthly screener run
        self._stocks = get_options_universe()
        log.info(
            "universe_loaded",
            total=len(self._stocks),
            growth=len(self.growth_stocks),
            dividend=len(self.dividend_stocks),
            markets=list(self.markets),
        )

    @property
    def all_symbols(self) -> list[str]:
        return [s.symbol for s in self._stocks]

    @property
    def growth_stocks(self) -> list[StockEntry]:
        return [s for s in self._stocks if s.category == "growth"]

    @property
    def dividend_stocks(self) -> list[StockEntry]:
        return [s for s in self._stocks if s.category == "dividend"]

    # Non-USD currencies that trade on European exchanges
    _EU_CURRENCIES = {"EUR", "CHF", "GBP", "NOK", "SEK", "DKK"}
    # Currencies that trade on Asian/Pacific exchanges
    _ASIA_CURRENCIES = {"JPY", "HKD", "KRW", "AUD", "SGD", "TWD", "INR", "IDR"}
    # All non-US currencies handled by regional virtual exchanges
    _REGIONAL_CURRENCIES = _EU_CURRENCIES | _ASIA_CURRENCIES

    @property
    def markets(self) -> set[str]:
        """All unique exchanges, plus virtual regional exchanges for SMART stocks."""
        exchanges = {s.exchange for s in self._stocks}
        if "SMART" in exchanges:
            currencies = {s.currency for s in self._stocks if s.exchange == "SMART"}
            if currencies & self._EU_CURRENCIES:
                exchanges.add("SMART_EU")
            if currencies & self._ASIA_CURRENCIES:
                exchanges.add("SMART_ASIA")
        return exchanges

    def symbols_for_market(self, exchange: str) -> list[str]:
        """Get all symbols that trade on a given exchange."""
        if exchange == "SMART_EU":
            return [s.symbol for s in self._stocks
                    if s.exchange == "SMART" and s.currency in self._EU_CURRENCIES]
        if exchange == "SMART_ASIA":
            return [s.symbol for s in self._stocks
                    if s.exchange == "SMART" and s.currency in self._ASIA_CURRENCIES]
        if exchange == "SMART":
            return [s.symbol for s in self._stocks
                    if s.exchange == "SMART" and s.currency not in self._REGIONAL_CURRENCIES]
        return [s.symbol for s in self._stocks if s.exchange == exchange]

    def stocks_for_market(self, exchange: str) -> list[StockEntry]:
        """Get all StockEntry objects for a given exchange."""
        if exchange == "SMART_EU":
            return [s for s in self._stocks
                    if s.exchange == "SMART" and s.currency in self._EU_CURRENCIES]
        if exchange == "SMART_ASIA":
            return [s for s in self._stocks
                    if s.exchange == "SMART" and s.currency in self._ASIA_CURRENCIES]
        if exchange == "SMART":
            return [s for s in self._stocks
                    if s.exchange == "SMART" and s.currency not in self._REGIONAL_CURRENCIES]
        return [s for s in self._stocks if s.exchange == exchange]

    def get_market_session(self, exchange: str) -> tuple[str, int, int, int, int] | None:
        """Get (timezone, open_h, open_m, close_h, close_m) for an exchange."""
        return self.MARKET_SESSIONS.get(exchange)

    def get_stock(self, symbol: str) -> StockEntry | None:
        for s in self._stocks:
            if s.symbol == symbol:
                return s
        return None

    def get_sector(self, symbol: str) -> str | None:
        stock = self.get_stock(symbol)
        return stock.sector if stock else None

    def get_exchange(self, symbol: str) -> str:
        stock = self.get_stock(symbol)
        return stock.exchange if stock else "SMART"

    def get_options_exchange(self, symbol: str) -> str:
        """Get the options/derivatives exchange for a symbol."""
        stock = self.get_stock(symbol)
        return stock.opt_exchange if stock else "SMART"

    def get_currency(self, symbol: str) -> str:
        stock = self.get_stock(symbol)
        return stock.currency if stock else "USD"

    def get_contract_size(self, symbol: str) -> int:
        stock = self.get_stock(symbol)
        return stock.contract_size if stock else 100

    def symbols_in_sector(self, sector: str) -> list[str]:
        return [s.symbol for s in self._stocks if s.sector == sector]
