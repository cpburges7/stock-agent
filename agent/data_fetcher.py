"""
Assembles the full data packet that gets sent to brain.py / Claude.
Calls screener → technical → news and merges with current portfolio state from logger.
"""

import logging

import yfinance as yf

from agent import logger as trade_logger
from agent.news import get_news
from agent.screener import build_watchlist
from agent.technical import get_technicals

log = logging.getLogger(__name__)


def get_current_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch the most recent close price for each ticker via yfinance."""
    if not tickers:
        return {}
    try:
        data = yf.download(
            tickers,
            period="2d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        closes = data.get("Close") if hasattr(data, "get") else data["Close"]
        if closes is None or closes.empty:
            return {}
        if not hasattr(closes, "columns"):
            closes = closes.to_frame(name=tickers[0])
        prices = {}
        for ticker in tickers:
            if ticker in closes.columns:
                val = closes[ticker].dropna()
                if not val.empty:
                    prices[ticker] = round(float(val.iloc[-1]), 2)
        return prices
    except Exception as e:
        log.error("Price fetch failed: %s", e)
        return {}


def build_data_packet(portfolio_override: dict | None = None) -> dict:
    """
    Build and return the complete data packet for a morning analysis run.

    portfolio_override: optional dict with keys cash, total_value, open_positions, open_shorts.
    If not provided, values are read from logs/trades.json (logger).

    Returns the packet dict ready to pass to brain.run_analysis().
    """

    # 1. Dynamic watchlist
    log.info("Building watchlist...")
    watchlist, earnings_today, earnings_tomorrow = build_watchlist()
    log.info("Watchlist: %d tickers | Earnings today: %s", len(watchlist), earnings_today)

    # 2. Technical indicators (batch fetch)
    log.info("Fetching technicals for %d tickers...", len(watchlist))
    market_data = get_technicals(watchlist)
    log.info("Technicals ready: %d tickers", len(market_data))

    # 3. News
    log.info("Fetching news...")
    news = get_news(watchlist)
    log.info("News ready: %d sources covered", len(news))

    # 4. Portfolio state
    if portfolio_override:
        portfolio = portfolio_override
    else:
        portfolio = trade_logger.get_portfolio_state()
        log.info(
            "Using persisted portfolio: cash=%.2f, total=%.2f",
            portfolio.get("cash", 0),
            portfolio.get("total_value", 0),
        )

    # 5. Market regime (single batch yfinance call; failures return a safe default)
    log.info("Fetching market regime...")
    try:
        from agent.regime import get_market_regime
        regime = get_market_regime()
    except Exception as e:
        log.error("Regime fetch failed: %s", e)
        regime = None

    return {
        "portfolio": portfolio,
        "market_data": market_data,
        "news": news,
        "earnings_today": earnings_today,
        "earnings_tomorrow": earnings_tomorrow,
        "regime": regime,
    }


if __name__ == "__main__":
    import json
    import os

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    packet = build_data_packet()
    # Print a summary without the full news/market_data blobs
    print(f"\nWatchlist tickers ({len(packet['market_data'])}):")
    print(", ".join(packet["market_data"].keys()))
    print(f"\nEarnings today: {packet['earnings_today']}")
    print(f"Earnings tomorrow: {packet['earnings_tomorrow']}")
    print(f"\nPortfolio cash: ${packet['portfolio']['cash']:,.2f}")
    print(f"Open positions: {len(packet['portfolio']['open_positions'])}")
    print(f"Open shorts:    {len(packet['portfolio']['open_shorts'])}")
