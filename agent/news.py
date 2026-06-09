"""
Multi-source financial news aggregator.
Priority: Alpaca Markets API → NewsAPI.org → RSS fallback (Yahoo Finance / MarketWatch / Reuters)
Returns per-ticker headline lists with source, time, and sentiment flag.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import feedparser
import requests

logger = logging.getLogger(__name__)

_POSITIVE_WORDS = {
    "beat", "beats", "rally", "rallied", "surge", "surged", "gain", "gained",
    "record", "upgrade", "upgraded", "buy", "strong", "bullish", "outperform",
    "raise", "raised", "above", "exceed", "exceeded", "profit", "growth",
    "breakthrough", "soar", "soared", "jump", "jumped", "top",
}
_NEGATIVE_WORDS = {
    "miss", "missed", "drop", "dropped", "decline", "declined", "fall", "fell",
    "downgrade", "downgraded", "sell", "weak", "bearish", "underperform",
    "cut", "cuts", "below", "loss", "warning", "warns", "layoff", "layoffs",
    "recall", "investigation", "lawsuit", "fine", "fined", "plunge", "plunged",
    "slump", "slumped", "disappoint", "disappointed",
}


def _sentiment(headline: str) -> str:
    words = set(headline.lower().split())
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%H:%M ET")


def _from_alpaca(tickers: list[str], limit_per_ticker: int = 3) -> dict[str, list[dict]]:
    """Fetch news from Alpaca Markets Data API v1beta1."""
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        return {}

    symbols = ",".join(tickers)
    url = "https://data.alpaca.markets/v1beta1/news"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params = {
        "symbols": symbols,
        "limit": min(len(tickers) * limit_per_ticker, 50),
        "sort": "desc",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json().get("news", [])
    except Exception as e:
        logger.warning("Alpaca news fetch failed: %s", e)
        return {}

    out: dict[str, list[dict]] = {}
    for article in articles:
        headline = article.get("headline", "")
        source = article.get("source", "Alpaca")
        published = article.get("created_at", "")
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            time_str = _fmt_time(dt.astimezone(timezone.utc))
        except Exception:
            time_str = ""

        item = {
            "headline": headline,
            "source": source,
            "time": time_str,
            "sentiment": _sentiment(headline),
        }

        for sym in article.get("symbols", []):
            if sym in tickers:
                out.setdefault(sym, [])
                if len(out[sym]) < limit_per_ticker:
                    out[sym].append(item)

    logger.info("Alpaca: got news for %d tickers", len(out))
    return out


def _from_newsapi(tickers: list[str], limit_per_ticker: int = 3) -> dict[str, list[dict]]:
    """Fetch news from NewsAPI.org (free tier: 100 req/day)."""
    api_key = os.getenv("NEWS_API_KEY")
    if not api_key:
        return {}

    out: dict[str, list[dict]] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    for ticker in tickers:
        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": f"{ticker} stock",
                    "apiKey": api_key,
                    "sortBy": "publishedAt",
                    "language": "en",
                    "pageSize": limit_per_ticker + 2,
                    "from": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                timeout=10,
            )
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
        except Exception as e:
            logger.warning("NewsAPI fetch failed for %s: %s", ticker, e)
            continue

        items = []
        for article in articles:
            headline = article.get("title", "")
            if not headline or "[Removed]" in headline:
                continue
            source = article.get("source", {}).get("name", "NewsAPI")
            published = article.get("publishedAt", "")
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                time_str = _fmt_time(dt)
            except Exception:
                time_str = ""
            items.append({
                "headline": headline,
                "source": source,
                "time": time_str,
                "sentiment": _sentiment(headline),
            })
            if len(items) >= limit_per_ticker:
                break

        if items:
            out[ticker] = items

    logger.info("NewsAPI: got news for %d tickers", len(out))
    return out


def _from_rss(tickers: list[str], limit_per_ticker: int = 3) -> dict[str, list[dict]]:
    """RSS fallback — Yahoo Finance per-ticker feed."""
    out: dict[str, list[dict]] = {}

    for ticker in tickers:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        try:
            feed = feedparser.parse(url)
            items = []
            for entry in feed.entries[:limit_per_ticker]:
                headline = entry.get("title", "")
                published = entry.get("published_parsed")
                if published:
                    dt = datetime(*published[:6], tzinfo=timezone.utc)
                    time_str = _fmt_time(dt)
                else:
                    time_str = ""
                items.append({
                    "headline": headline,
                    "source": "Yahoo Finance",
                    "time": time_str,
                    "sentiment": _sentiment(headline),
                })
            if items:
                out[ticker] = items
        except Exception as e:
            logger.warning("RSS fetch failed for %s: %s", ticker, e)

    logger.info("RSS: got news for %d tickers", len(out))
    return out


def _from_rss_market() -> list[dict]:
    """General market headlines from MarketWatch and Reuters RSS."""
    feeds = [
        ("https://feeds.marketwatch.com/marketwatch/marketpulse/", "MarketWatch"),
        ("https://feeds.reuters.com/reuters/businessNews", "Reuters"),
    ]
    items = []
    for url, source in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:4]:
                headline = entry.get("title", "")
                published = entry.get("published_parsed")
                time_str = ""
                if published:
                    dt = datetime(*published[:6], tzinfo=timezone.utc)
                    time_str = _fmt_time(dt)
                items.append({
                    "headline": headline,
                    "source": source,
                    "time": time_str,
                    "sentiment": _sentiment(headline),
                })
        except Exception as e:
            logger.warning("Market RSS fetch failed (%s): %s", source, e)
    return items[:8]


def get_news(tickers: list[str]) -> dict[str, list[dict]]:
    """
    Fetch news for all tickers using the priority chain:
      Alpaca → NewsAPI (for missing tickers) → RSS (for still-missing tickers)
    Also adds a "market" key with general market headlines.
    """
    out: dict[str, list[dict]] = {}

    # Layer 1: Alpaca
    alpaca_results = _from_alpaca(tickers)
    out.update(alpaca_results)

    # Layer 2: NewsAPI for tickers not covered by Alpaca
    missing = [t for t in tickers if t not in out]
    if missing:
        newsapi_results = _from_newsapi(missing)
        out.update(newsapi_results)

    # Layer 3: RSS fallback for still-missing tickers
    still_missing = [t for t in tickers if t not in out]
    if still_missing:
        rss_results = _from_rss(still_missing)
        out.update(rss_results)

    # General market news
    out["market"] = _from_rss_market()

    covered = sum(1 for t in tickers if t in out)
    logger.info("News coverage: %d/%d tickers", covered, len(tickers))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sample = ["NVDA", "AAPL", "SPY"]
    results = get_news(sample)
    for ticker, headlines in results.items():
        print(f"\n{ticker}:")
        for h in headlines:
            print(f"  [{h['source']} {h['time']}] {h['headline']} ({h['sentiment']})")
