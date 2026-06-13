"""
Hourly position monitor. Reads open positions from logger, fetches live prices,
and returns a list of exit alerts. Called from /monitor endpoint and runnable standalone.
"""

import logging
from datetime import datetime, time, timezone

import yfinance as yf

from agent import logger as trade_logger

log = logging.getLogger(__name__)

# ET offset approximation (UTC-4 during EDT, UTC-5 during EST)
# Railway runs UTC; we compare against the 4 PM close regardless of DST precision needed here.
MARKET_CLOSE = time(20, 0)   # 4 PM ET = 20:00 UTC (EDT) / 21:00 UTC (EST)
MARKET_CLOSE_EST = time(21, 0)
CLOSE_WARNING_MINUTES = 30


def _get_current_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch latest close prices for a list of tickers via yfinance."""
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

        # Normalize to DataFrame for single-ticker edge case
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
        log.error("Price fetch failed in monitor: %s", e)
        return {}


def _is_near_close() -> bool:
    """Return True if within CLOSE_WARNING_MINUTES of 4 PM ET."""
    now_utc = datetime.now(timezone.utc).time()
    # Check EDT window (UTC-4): close = 20:00 UTC
    close_edt = time(20, 0)
    warn_edt = time(19, 60 - CLOSE_WARNING_MINUTES) if CLOSE_WARNING_MINUTES < 60 else time(19, 0)
    # Simplified: warn if 19:30–20:00 UTC (covers EDT) or 20:30–21:00 UTC (EST)
    in_edt_window = time(19, 30) <= now_utc <= time(20, 0)
    in_est_window = time(20, 30) <= now_utc <= time(21, 0)
    return in_edt_window or in_est_window


def _days_held(opened_date_str: str) -> int:
    """Calculate calendar days since a position was opened."""
    try:
        opened = datetime.fromisoformat(opened_date_str).date()
        today = datetime.now(timezone.utc).date()
        return (today - opened).days
    except Exception:
        return 0


def _check_long(pos: dict, current_price: float) -> dict | None:
    """Return an alert dict if a long position needs action, else None."""
    ticker = pos["ticker"]
    entry = pos.get("entry_price", 0)
    target = pos.get("target_price", float("inf"))
    stop = pos.get("stop_loss", 0)
    horizon = pos.get("time_horizon_days", 5)
    opened = pos.get("entry_date") or pos.get("opened_date", "")
    days = _days_held(opened) if opened else pos.get("days_held", 0)
    pnl_pct = round((current_price - entry) / entry * 100, 2) if entry else 0

    if current_price >= target:
        return {
            "ticker": ticker,
            "alert_type": "TAKE_PROFIT",
            "action": "SELL",
            "current_price": current_price,
            "entry_price": entry,
            "pnl_pct": pnl_pct,
            "reason": f"Price ${current_price} hit target ${target} (+{pnl_pct:.1f}%)",
        }
    if current_price <= stop:
        return {
            "ticker": ticker,
            "alert_type": "STOP_LOSS",
            "action": "SELL",
            "current_price": current_price,
            "entry_price": entry,
            "pnl_pct": pnl_pct,
            "reason": f"Price ${current_price} hit stop ${stop} ({pnl_pct:.1f}%)",
        }
    if days >= horizon:
        return {
            "ticker": ticker,
            "alert_type": "TIME_STOP",
            "action": "SELL",
            "current_price": current_price,
            "entry_price": entry,
            "pnl_pct": pnl_pct,
            "reason": f"Held {days}d vs {horizon}d horizon. P&L: {pnl_pct:+.1f}%. Review and close.",
        }
    if _is_near_close() and pos.get("is_day_trade"):
        return {
            "ticker": ticker,
            "alert_type": "MARKET_CLOSE",
            "action": "SELL",
            "current_price": current_price,
            "entry_price": entry,
            "pnl_pct": pnl_pct,
            "reason": f"Market closes in ~{CLOSE_WARNING_MINUTES}min. Day trade — consider closing.",
        }
    return None


def _check_short(pos: dict, current_price: float) -> dict | None:
    """Return an alert dict if a short position needs action, else None."""
    ticker = pos["ticker"]
    entry = pos.get("entry_price", 0)
    cover = pos.get("cover_target", 0)
    stop = pos.get("stop_loss", float("inf"))
    horizon = pos.get("time_horizon_days", 6)
    opened = pos.get("entry_date") or pos.get("opened_date", "")
    days = _days_held(opened) if opened else pos.get("days_held", 0)
    pnl_pct = round((entry - current_price) / entry * 100, 2) if entry else 0

    if current_price <= cover:
        return {
            "ticker": ticker,
            "alert_type": "TAKE_PROFIT",
            "action": "COVER",
            "current_price": current_price,
            "entry_price": entry,
            "pnl_pct": pnl_pct,
            "reason": f"Price ${current_price} hit cover target ${cover} (+{pnl_pct:.1f}%)",
        }
    if current_price >= stop:
        return {
            "ticker": ticker,
            "alert_type": "STOP_LOSS",
            "action": "COVER",
            "current_price": current_price,
            "entry_price": entry,
            "pnl_pct": pnl_pct,
            "reason": f"Price ${current_price} hit stop ${stop} ({pnl_pct:.1f}%)",
        }
    if days >= horizon:
        return {
            "ticker": ticker,
            "alert_type": "TIME_STOP",
            "action": "COVER",
            "current_price": current_price,
            "entry_price": entry,
            "pnl_pct": pnl_pct,
            "reason": f"Held {days}d vs {horizon}d horizon. P&L: {pnl_pct:+.1f}%. Review and cover.",
        }
    return None


def check_positions() -> list[dict]:
    """
    Main monitor entry point. Reads open positions from logger, fetches prices,
    and returns a list of alert dicts. Empty list = nothing to act on.
    """
    portfolio = trade_logger.get_portfolio_state()
    open_positions = portfolio.get("open_positions", [])
    open_shorts = portfolio.get("open_shorts", [])

    if not open_positions and not open_shorts:
        log.info("No open positions to monitor.")
        return []

    all_tickers = list({
        pos["ticker"]
        for pos in (open_positions + open_shorts)
        if "ticker" in pos
    })

    prices = _get_current_prices(all_tickers)
    log.info("Monitoring %d positions | prices fetched: %d", len(all_tickers), len(prices))

    alerts = []

    for pos in open_positions:
        ticker = pos.get("ticker")
        if not ticker or ticker not in prices:
            continue
        alert = _check_long(pos, prices[ticker])
        if alert:
            alerts.append(alert)

    for pos in open_shorts:
        ticker = pos.get("ticker")
        if not ticker or ticker not in prices:
            continue
        alert = _check_short(pos, prices[ticker])
        if alert:
            alerts.append(alert)

    log.info("Monitor complete: %d alerts", len(alerts))
    return alerts


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    alerts = check_positions()
    if alerts:
        for a in alerts:
            print(f"[{a['alert_type']}] {a['ticker']} {a['action']} @ ${a['current_price']} | {a['reason']}")
    else:
        print("No alerts — all positions within parameters.")
