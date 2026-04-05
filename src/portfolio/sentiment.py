"""
News sentiment scoring for portfolio entry decisions.

Uses Finnhub company-news (free tier) to fetch recent headlines,
scores them with a keyword list, and returns a sentiment signal.

Score range: -1.0 (very negative) to +1.0 (very positive)
Threshold: score < -0.3 = negative sentiment, delay entry

Free tier: 60 API calls/min — one call per stock per day is safe.
"""
from __future__ import annotations

import requests
from datetime import datetime, timedelta
from src.core.logger import get_logger

log = get_logger(__name__)

# Positive keywords — indicate bullish news
POSITIVE_WORDS = {
    "beat", "beats", "record", "rally", "surge", "surges", "upgrade",
    "upgraded", "growth", "profit", "profitable", "strong", "raises",
    "raised", "buyback", "dividend", "partnership", "wins", "awarded",
    "expands", "expansion", "exceeds", "outperform", "buy", "bullish",
    "breakthrough", "innovative", "launches", "deal", "agreement",
    "acquisition", "acquires", "revenue", "earnings", "positive",
}

# Negative keywords — indicate bearish news
NEGATIVE_WORDS = {
    "miss", "misses", "missed", "cut", "cuts", "downgrade", "downgraded",
    "loss", "losses", "losing", "decline", "declines", "declining", "fell",
    "falls", "crash", "crashes", "lawsuit", "investigation", "probe",
    "fine", "fined", "penalty", "warning", "warns", "layoffs", "layoff",
    "recall", "fraud", "scandal", "risk", "risks", "debt", "default",
    "bankruptcy", "bankrupt", "sell", "bearish", "concern", "concerns",
    "disappoints", "disappointing", "weak", "weakens", "slump", "slumps",
    "plunges", "plunge", "halted", "suspended", "under", "below",
}


def get_news_sentiment(symbol: str, api_key: str, days: int = 7) -> dict:
    """
    Fetch recent news for a symbol and return a sentiment score.

    Returns:
        {
            "score": float,       # -1.0 to +1.0
            "signal": str,        # "positive", "neutral", "negative"
            "articles": int,      # number of articles scored
            "positive": int,      # positive headlines count
            "negative": int,      # negative headlines count
        }
    """
    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    try:
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": symbol, "from": from_date, "to": to_date, "token": api_key},
            timeout=8,
        )
        if r.status_code != 200:
            log.debug("sentiment_fetch_failed", symbol=symbol, status=r.status_code)
            return _neutral()

        articles = r.json()
        if not articles:
            return _neutral()

        pos = 0
        neg = 0
        for article in articles:
            headline = (article.get("headline") or "").lower()
            words = set(headline.split())
            pos += len(words & POSITIVE_WORDS)
            neg += len(words & NEGATIVE_WORDS)

        total = pos + neg
        if total == 0:
            return _neutral()

        score = round((pos - neg) / total, 3)
        if score > 0.1:
            signal = "positive"
        elif score < -0.1:
            signal = "negative"
        else:
            signal = "neutral"

        log.debug("sentiment_scored", symbol=symbol, score=score,
                  signal=signal, articles=len(articles), pos=pos, neg=neg)

        return {
            "score": score,
            "signal": signal,
            "articles": len(articles),
            "positive": pos,
            "negative": neg,
        }

    except Exception as e:
        log.debug("sentiment_error", symbol=symbol, error=str(e))
        return _neutral()


def _neutral() -> dict:
    return {"score": 0.0, "signal": "neutral", "articles": 0, "positive": 0, "negative": 0}
