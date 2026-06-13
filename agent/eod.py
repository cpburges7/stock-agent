"""
End-of-day portfolio snapshot. Reads open positions, fetches live prices,
and returns a P&L summary with position-level status flags.
"""

import logging
from datetime import datetime, timezone

from agent import logger as trade_logger
from agent.data_fetcher import get_current_prices

log = logging.getLogger(__name__)

STARTING_VALUE = 50_008.22
NEAR_THRESHOLD = 0.02  # 2% from target/stop triggers near_* status


def _days_held(entry_date_str: str) -> int:
    try:
        entry = datetime.fromisoformat(entry_date_str).date()
        today = datetime.now(timezone.utc).date()
        return (today - entry).days
    except Exception:
        return 0


def _position_status(pos: dict, current_price: float, direction: str) -> str:
    if direction == "LONG":
        target = pos.get("target_price")
        stop = pos.get("stop_loss", 0)
        if target and current_price >= target:
            return "target_hit"
        if stop and current_price <= stop:
            return "stop_hit"
        if target and current_price >= target * (1 - NEAR_THRESHOLD):
            return "near_target"
        if stop and current_price <= stop * (1 + NEAR_THRESHOLD):
            return "near_stop"
    else:
        cover = pos.get("cover_target", 0)
        stop = pos.get("stop_loss", float("inf"))
        if cover and current_price <= cover:
            return "target_hit"
        if current_price >= stop:
            return "stop_hit"
        if cover and current_price <= cover * (1 + NEAR_THRESHOLD):
            return "near_target"
        if current_price >= stop * (1 - NEAR_THRESHOLD):
            return "near_stop"
    return "on_track"


def build_eod_snapshot() -> dict:
    """
    Build the EOD snapshot dict, save it to snapshots.json, and return it.
    estimated_total_value = cash + Σ(long market value) - Σ(short liability)
    """
    portfolio = trade_logger.get_portfolio_state()
    open_positions = portfolio.get("open_positions", [])
    open_shorts = portfolio.get("open_shorts", [])
    cash = portfolio.get("cash", STARTING_VALUE)
    today = datetime.now(timezone.utc).date().isoformat()

    if not open_positions and not open_shorts:
        prev = trade_logger.get_previous_snapshot()
        prev_value = prev.get("estimated_total_value", STARTING_VALUE) if prev else STARTING_VALUE
        snapshot = {
            "snapshot_date": today,
            "estimated_total_value": round(cash, 2),
            "starting_value": STARTING_VALUE,
            "total_unrealized_pnl_dollars": 0.0,
            "total_unrealized_pnl_pct": 0.0,
            "day_change_dollars": round(cash - prev_value, 2),
            "day_change_pct": round((cash - prev_value) / prev_value * 100, 2) if prev_value else 0.0,
            "cash": round(cash, 2),
            "positions": [],
            "best_performer": None,
            "worst_performer": None,
            "positions_near_target": [],
            "positions_near_stop": [],
            "message": "No open positions",
        }
        trade_logger.save_snapshot(snapshot)
        return snapshot

    all_tickers = list({
        pos["ticker"]
        for pos in (open_positions + open_shorts)
        if "ticker" in pos
    })
    prices = get_current_prices(all_tickers)
    log.info("EOD snapshot: fetched prices for %d/%d tickers", len(prices), len(all_tickers))

    position_rows = []
    total_pnl = 0.0
    long_market_value = 0.0
    short_liability = 0.0

    for pos in open_positions:
        ticker = pos.get("ticker", "")
        entry = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        entry_date = pos.get("entry_date") or pos.get("opened_date", "")

        if ticker not in prices:
            position_rows.append({
                "ticker": ticker,
                "direction": "LONG",
                "shares": shares,
                "entry_price": entry,
                "error": "price unavailable",
            })
            continue

        current = prices[ticker]
        pnl_dollars = round((current - entry) * shares, 2)
        pnl_pct = round((current - entry) / entry * 100, 2) if entry else 0.0
        total_pnl += pnl_dollars
        long_market_value += current * shares

        position_rows.append({
            "ticker": ticker,
            "direction": "LONG",
            "shares": shares,
            "entry_price": entry,
            "current_price": current,
            "unrealized_pnl_dollars": pnl_dollars,
            "unrealized_pnl_pct": pnl_pct,
            "days_held": _days_held(entry_date),
            "status": _position_status(pos, current, "LONG"),
        })

    for pos in open_shorts:
        ticker = pos.get("ticker", "")
        entry = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        entry_date = pos.get("entry_date") or pos.get("opened_date", "")

        if ticker not in prices:
            position_rows.append({
                "ticker": ticker,
                "direction": "SHORT",
                "shares": shares,
                "entry_price": entry,
                "error": "price unavailable",
            })
            continue

        current = prices[ticker]
        pnl_dollars = round((entry - current) * shares, 2)
        pnl_pct = round((entry - current) / entry * 100, 2) if entry else 0.0
        total_pnl += pnl_dollars
        short_liability += current * shares

        position_rows.append({
            "ticker": ticker,
            "direction": "SHORT",
            "shares": shares,
            "entry_price": entry,
            "current_price": current,
            "unrealized_pnl_dollars": pnl_dollars,
            "unrealized_pnl_pct": pnl_pct,
            "days_held": _days_held(entry_date),
            "status": _position_status(pos, current, "SHORT"),
        })

    estimated_total = round(cash + long_market_value - short_liability, 2)
    total_pnl = round(total_pnl, 2)
    total_pnl_pct = round(total_pnl / STARTING_VALUE * 100, 2)

    prev = trade_logger.get_previous_snapshot()
    prev_value = prev.get("estimated_total_value", STARTING_VALUE) if prev else STARTING_VALUE
    day_change = round(estimated_total - prev_value, 2)
    day_change_pct = round(day_change / prev_value * 100, 2) if prev_value else 0.0

    priced = [r for r in position_rows if "unrealized_pnl_pct" in r]
    best = max(priced, key=lambda r: r["unrealized_pnl_pct"])["ticker"] if priced else None
    worst = min(priced, key=lambda r: r["unrealized_pnl_pct"])["ticker"] if priced else None
    near_target = [r["ticker"] for r in priced if r.get("status") in ("near_target", "target_hit")]
    near_stop = [r["ticker"] for r in priced if r.get("status") in ("near_stop", "stop_hit")]

    snapshot = {
        "snapshot_date": today,
        "estimated_total_value": estimated_total,
        "starting_value": STARTING_VALUE,
        "total_unrealized_pnl_dollars": total_pnl,
        "total_unrealized_pnl_pct": total_pnl_pct,
        "day_change_dollars": day_change,
        "day_change_pct": day_change_pct,
        "cash": round(cash, 2),
        "positions": position_rows,
        "best_performer": best,
        "worst_performer": worst,
        "positions_near_target": near_target,
        "positions_near_stop": near_stop,
    }

    trade_logger.save_snapshot(snapshot)
    return snapshot


if __name__ == "__main__":
    import json
    import logging as _logging
    from dotenv import load_dotenv
    load_dotenv()
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")
    result = build_eod_snapshot()
    print(json.dumps(result, indent=2))
