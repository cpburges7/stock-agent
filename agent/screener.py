"""
Dynamic watchlist builder. Runs every morning before data_fetcher.
Combines base universe + most-active Yahoo screen + unusual volume + earnings calendar + sector ETF momentum.
Output: deduplicated list of up to 50 tickers, all priced >= $3 with avg volume >= 500k.
"""

import logging
from datetime import date, timedelta

import yfinance as yf

logger = logging.getLogger(__name__)

BASE = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMD", "META",
    "GOOGL", "AMZN", "PLTR", "COIN", "XOM", "JPM", "GS", "SOFI",
]

SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLC", "XLY", "XLP", "XLB", "XLRE", "XLU"]

MAX_WATCHLIST = 50
MIN_PRICE = 3.0
MIN_AVG_VOLUME = 500_000


def _get_most_actives(n: int = 20) -> list[str]:
    """Top N tickers by volume from Yahoo Finance most-actives screen."""
    try:
        result = yf.screen("most_actives", size=n)
        quotes = result.get("quotes", [])
        return [q["symbol"] for q in quotes if "symbol" in q]
    except Exception as e:
        logger.warning("most_actives screen failed: %s", e)
        return []


def _get_unusual_volume(tickers: list[str]) -> list[str]:
    """Return tickers whose today's volume exceeds 2x their 20-day average volume."""
    flagged = []
    try:
        data = yf.download(
            tickers,
            period="22d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        volume = data.get("Volume")
        if volume is None or volume.empty:
            return flagged

        # Handle single-ticker edge case (yfinance returns a Series, not DataFrame)
        if hasattr(volume, "squeeze"):
            volume = volume.squeeze()
            if not hasattr(volume, "columns"):
                volume = volume.to_frame(name=tickers[0] if len(tickers) == 1 else "unknown")

        for ticker in tickers:
            if ticker not in volume.columns:
                continue
            series = volume[ticker].dropna()
            if len(series) < 2:
                continue
            avg_20 = series.iloc[:-1].tail(20).mean()
            today_vol = series.iloc[-1]
            if avg_20 > 0 and today_vol >= 2 * avg_20:
                flagged.append(ticker)
    except Exception as e:
        logger.warning("unusual volume check failed: %s", e)
    return flagged


def _get_earnings_tickers(days_ahead: int = 2) -> tuple[list[str], list[str]]:
    """
    Return (earnings_today, earnings_tomorrow) using yfinance earnings calendar.
    Falls back to empty lists on any failure.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    earnings_today: list[str] = []
    earnings_tomorrow: list[str] = []

    # yfinance doesn't have a direct earnings-calendar endpoint for arbitrary tickers,
    # so we check the BASE universe individually — fast enough for 16 symbols.
    for ticker in BASE:
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None or cal.empty:
                continue
            # calendar index contains dates; columns include 'Earnings Date'
            if "Earnings Date" in cal.columns:
                for ed in cal["Earnings Date"]:
                    ed_date = ed.date() if hasattr(ed, "date") else ed
                    if ed_date == today:
                        earnings_today.append(ticker)
                    elif ed_date == tomorrow:
                        earnings_tomorrow.append(ticker)
            elif not cal.empty:
                # Some versions return a different shape — check index
                for idx_val in cal.index:
                    try:
                        ed_date = idx_val.date() if hasattr(idx_val, "date") else None
                        if ed_date == today:
                            earnings_today.append(ticker)
                        elif ed_date == tomorrow:
                            earnings_tomorrow.append(ticker)
                    except Exception:
                        pass
        except Exception:
            pass

    return list(set(earnings_today)), list(set(earnings_tomorrow))


def _get_top_sector_movers(n: int = 2) -> list[str]:
    """Return the top N sector ETFs by absolute 1-day percentage change."""
    try:
        data = yf.download(
            SECTORS,
            period="2d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        closes = data.get("Close")
        if closes is None or closes.empty or len(closes) < 2:
            return []
        pct_changes = closes.pct_change().iloc[-1].abs().dropna()
        top = pct_changes.nlargest(n).index.tolist()
        return top
    except Exception as e:
        logger.warning("sector mover check failed: %s", e)
        return []


def _filter_tickers(tickers: list[str]) -> list[str]:
    """
    Remove tickers that don't meet minimum price ($3) or average volume (500k) thresholds.
    Uses a single batch download for efficiency.
    """
    if not tickers:
        return []
    try:
        data = yf.download(
            tickers,
            period="22d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        closes = data.get("Close")
        volumes = data.get("Volume")
        if closes is None or volumes is None:
            return tickers  # can't filter, return all and let downstream handle it

        # Normalize to DataFrame for single-ticker edge case
        if not hasattr(closes, "columns"):
            closes = closes.to_frame(name=tickers[0])
            volumes = volumes.to_frame(name=tickers[0])

        valid = []
        for ticker in tickers:
            if ticker not in closes.columns:
                continue
            price = closes[ticker].dropna().iloc[-1] if not closes[ticker].dropna().empty else 0
            avg_vol = volumes[ticker].dropna().tail(20).mean() if not volumes[ticker].dropna().empty else 0
            if price >= MIN_PRICE and avg_vol >= MIN_AVG_VOLUME:
                valid.append(ticker)
        return valid
    except Exception as e:
        logger.warning("ticker filter failed: %s", e)
        return tickers  # fail open so the agent still runs


def build_watchlist() -> tuple[list[str], list[str], list[str]]:
    """
    Build and return (watchlist, earnings_today, earnings_tomorrow).

    watchlist: deduplicated, filtered list of up to MAX_WATCHLIST tickers.
    earnings_today / earnings_tomorrow: raw lists (not filtered by price/volume —
    earnings tickers are always included regardless of liquidity).
    """
    combined: list[str] = list(BASE)  # start with base universe

    # Layer 1: most active
    actives = _get_most_actives(20)
    combined.extend(actives)
    logger.info("Most actives added: %s", actives)

    # Layer 2: unusual volume (run on base + actives combined so far)
    unique_so_far = list(dict.fromkeys(combined))
    unusual = _get_unusual_volume(unique_so_far)
    # unusual tickers are already in combined; flagging them is handled by data_fetcher
    logger.info("Unusual volume tickers: %s", unusual)

    # Layer 3: earnings
    earnings_today, earnings_tomorrow = _get_earnings_tickers()
    combined.extend(earnings_today)
    combined.extend(earnings_tomorrow)
    logger.info("Earnings today: %s | tomorrow: %s", earnings_today, earnings_tomorrow)

    # Layer 4: top sector movers
    sector_movers = _get_top_sector_movers(2)
    combined.extend(sector_movers)
    logger.info("Top sector movers: %s", sector_movers)

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for t in combined:
        if t not in seen:
            seen.add(t)
            deduped.append(t)

    # Filter by price and volume, then cap at MAX_WATCHLIST
    filtered = _filter_tickers(deduped)

    # Always include earnings tickers even if they fail the filter
    # (they're high-signal regardless of normal liquidity)
    must_include = [t for t in earnings_today + earnings_tomorrow if t not in filtered]
    watchlist = (must_include + filtered)[:MAX_WATCHLIST]

    logger.info(
        "Watchlist built: %d tickers (from %d candidates after filter)",
        len(watchlist),
        len(deduped),
    )
    return watchlist, earnings_today, earnings_tomorrow


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    wl, et, etom = build_watchlist()
    print(f"\nWatchlist ({len(wl)} tickers):")
    print(", ".join(wl))
    print(f"\nEarnings today:    {et}")
    print(f"Earnings tomorrow: {etom}")
