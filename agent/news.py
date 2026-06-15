"""
Multi-source financial news aggregator.
Priority: Alpaca Markets API → NewsAPI.org → RSS fallback (Yahoo Finance / MarketWatch / Reuters)
Returns per-ticker headline lists with source, time, sentiment flag, AND freshness metadata.

CHANGES FROM YOUR ORIGINAL (all marked with #  <<< NEW or #  <<< CHANGED):
  1. Every article now carries a real datetime + age in hours + a FRESH/RECENT/STALE tag.
  2. _fmt_time() now includes the DATE, not just HH:MM — so the model can never mistake
     a 9-day-old article for today's news (this was the root cause of the inverted oil thesis).
  3. A hard freshness filter drops anything older than MAX_NEWS_AGE_HOURS before it can
     reach the prompt. Articles with no parseable timestamp are dropped (can't be trusted).
  4. get_news() returns a small "_meta" block so the brain can tell when news was thin/stale.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import feedparser
import requests

logger = logging.getLogger(__name__)

# ---- Freshness knobs --------------------------------------------------  #  <<< NEW
# Hard cutoff: any article older than this never reaches the prompt.
MAX_NEWS_AGE_HOURS = 36
# Articles newer than this are tagged FRESH (the model is told to weight these heavily).
FRESH_NEWS_AGE_HOURS = 18
# Market headlines can be a touch older and still be useful context.
MAX_MARKET_NEWS_AGE_HOURS = 48

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


def _fmt_time(dt: datetime | None) -> str:  #  <<< CHANGED — now includes the DATE
    """
    Format with full date + time so a stale article can NEVER masquerade as today's.
    Old version returned only 'HH:MM ET', which is what let 9-day-old Iran-deal news
    look like breaking news. Never go back to a time-only format here.
    """
    if dt is None:
        return "unknown-date"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _age_hours(dt: datetime | None) -> float | None:  #  <<< NEW
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    delta = (now - dt).total_seconds() / 3600.0
    return max(delta, 0.0)  # clock-skew guard


def _freshness_tag(age_hours: float | None) -> str:  #  <<< NEW
    if age_hours is None:
        return "UNDATED"
    if age_hours <= FRESH_NEWS_AGE_HOURS:
        return "FRESH"
    if age_hours <= MAX_NEWS_AGE_HOURS:
        return "RECENT"
    return "STALE"


def _make_item(headline: str, source: str, dt: datetime | None) -> dict:  #  <<< NEW helper
    """Build a single news item with full freshness metadata attached."""
    age = _age_hours(dt)
    return {
        "headline": headline,
        "source": source,
        "time": _fmt_time(dt),          # now date+time
        "age_hours": round(age, 1) if age is not None else None,
        "freshness": _freshness_tag(age),
        "sentiment": _sentiment(headline),
    }


def _is_fresh_enough(item: dict, max_age: int = MAX_NEWS_AGE_HOURS) -> bool:  #  <<< NEW
    """Drop STALE and UNDATED items — only FRESH/RECENT within max_age survive."""
    age = item.get("age_hours")
    if age is None:
        return False          # no timestamp = can't trust = drop
    return age <= max_age


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
            dt = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            dt = None

        item = _make_item(headline, source, dt)  #  <<< CHANGED
        if not _is_fresh_enough(item):           #  <<< NEW — drop stale BEFORE it spreads
            continue

        for sym in article.get("symbols", []):
            if sym in tickers:
                out.setdefault(sym, [])
                if len(out[sym]) < limit_per_ticker:
                    out[sym].append(item)

    logger.info("Alpaca: %d tickers with FRESH news", len(out))
    return out


def _from_newsapi(tickers: list[str], limit_per_ticker: int = 3) -> dict[str, list[dict]]:
    """Fetch news from NewsAPI.org (free tier: 100 req/day)."""
    api_key = os.getenv("NEWS_API_KEY")
    if not api_key:
        return {}

    out: dict[str, list[dict]] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_NEWS_AGE_HOURS)  #  <<< CHANGED (was 24h)

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
                dt = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                dt = None

            item = _make_item(headline, source, dt)  #  <<< CHANGED
            if not _is_fresh_enough(item):           #  <<< NEW
                continue
            items.append(item)
            if len(items) >= limit_per_ticker:
                break

        if items:
            out[ticker] = items

    logger.info("NewsAPI: %d tickers with FRESH news", len(out))
    return out


def _from_rss(tickers: list[str], limit_per_ticker: int = 3) -> dict[str, list[dict]]:
    """RSS fallback — Yahoo Finance per-ticker feed."""
    out: dict[str, list[dict]] = {}

    for ticker in tickers:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        try:
            feed = feedparser.parse(url)
            items = []
            for entry in feed.entries[:limit_per_ticker + 3]:  # grab a few extra, some will be stale
                headline = entry.get("title", "")
                published = entry.get("published_parsed")
                dt = datetime(*published[:6], tzinfo=timezone.utc) if published else None

                item = _make_item(headline, "Yahoo Finance", dt)  #  <<< CHANGED
                if not _is_fresh_enough(item):                    #  <<< NEW
                    continue
                items.append(item)
                if len(items) >= limit_per_ticker:
                    break
            if items:
                out[ticker] = items
        except Exception as e:
            logger.warning("RSS fetch failed for %s: %s", ticker, e)

    logger.info("RSS: %d tickers with FRESH news", len(out))
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
            for entry in feed.entries[:6]:
                headline = entry.get("title", "")
                published = entry.get("published_parsed")
                dt = datetime(*published[:6], tzinfo=timezone.utc) if published else None
                item = _make_item(headline, source, dt)  #  <<< CHANGED
                if not _is_fresh_enough(item, MAX_MARKET_NEWS_AGE_HOURS):  #  <<< NEW
                    continue
                items.append(item)
        except Exception as e:
            logger.warning("Market RSS fetch failed (%s): %s", source, e)
    # newest first
    items.sort(key=lambda i: i.get("age_hours") if i.get("age_hours") is not None else 1e9)
    return items[:8]


def get_news(tickers: list[str]) -> dict[str, list[dict]]:
    """
    Fetch FRESH news for all tickers using the priority chain:
      Alpaca → NewsAPI (for missing tickers) → RSS (for still-missing tickers)
    Adds a "market" key with general headlines and a "_meta" key with freshness stats
    so the brain/prompt can react when news is thin or stale.
    """
    out: dict[str, list[dict]] = {}

    alpaca_results = _from_alpaca(tickers)
    out.update(alpaca_results)

    missing = [t for t in tickers if t not in out]
    if missing:
        out.update(_from_newsapi(missing))

    still_missing = [t for t in tickers if t not in out]
    if still_missing:
        out.update(_from_rss(still_missing))

    out["market"] = _from_rss_market()

    # ---- freshness summary for downstream guardrails ----  #  <<< NEW
    ticker_items = [it for t, items in out.items() if t not in ("market", "_meta") for it in items]
    fresh_count = sum(1 for it in ticker_items if it.get("freshness") == "FRESH")
    covered = sum(1 for t in tickers if t in out and t not in ("market", "_meta"))
    newest_age = min((it["age_hours"] for it in ticker_items if it.get("age_hours") is not None),
                     default=None)

    out["_meta"] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tickers_requested": len(tickers),
        "tickers_covered": covered,
        "total_fresh_items": fresh_count,
        "newest_item_age_hours": round(newest_age, 1) if newest_age is not None else None,
        "news_is_thin": covered < max(1, len(tickers) // 2) or fresh_count == 0,
    }

    logger.info("News coverage: %d/%d tickers, %d fresh items, newest %.1fh old",
                covered, len(tickers), fresh_count,
                newest_age if newest_age is not None else -1)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sample = ["NVDA", "AAPL", "SPY"]
    results = get_news(sample)
    print("\n_meta:", results.get("_meta"))
    for ticker, headlines in results.items():
        if ticker == "_meta":
            continue
        print(f"\n{ticker}:")
        for h in headlines:
            print(f"  [{h['freshness']} | {h['time']} | {h['source']}] "
                  f"{h['headline']} ({h['sentiment']})")