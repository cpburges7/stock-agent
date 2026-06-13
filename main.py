"""
FastAPI entry point. Exposes /analyze, /monitor, /positions, /eod-snapshot,
/closed-positions, /calibration, /weekly-digest, /health.
All endpoints except /health require X-API-Key header matching AGENT_API_KEY in .env.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Stock Trading Agent", version="1.0.0")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def _verify_api_key(key: str | None = Security(_API_KEY_HEADER)) -> str:
    expected = os.getenv("AGENT_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AGENT_API_KEY not configured on server",
        )
    if key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key",
        )
    return key


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    triggered_by: str = Field(default="n8n")


class PortfolioState(BaseModel):
    cash: float = Field(..., description="Current cash balance in USD")
    total_value: float = Field(..., description="Total portfolio value in USD")
    open_positions: list[dict] = Field(default_factory=list)
    open_shorts: list[dict] = Field(default_factory=list)
    closed_today: list[dict] = Field(default_factory=list, description="Positions closed since last update")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
    }


@app.post("/analyze", dependencies=[Depends(_verify_api_key)])
def analyze(request: AnalyzeRequest):
    """
    Full morning analysis run. Builds dynamic watchlist, fetches technicals and news,
    runs Claude analysis, validates positions, saves to log. Called by n8n at 9:30 AM ET.
    Reads current portfolio state from logs/trades.json (set via POST /positions).
    """
    from agent.brain import run_analysis
    from agent.data_fetcher import build_data_packet

    try:
        data_packet = build_data_packet()
        result = run_analysis(data_packet=data_packet, triggered_by=request.triggered_by)
    except Exception as e:
        log.exception("Analysis failed")
        raise HTTPException(status_code=500, detail=str(e))

    return result


@app.post("/monitor", dependencies=[Depends(_verify_api_key)])
def monitor():
    """
    Hourly position monitor. Returns list of exit alerts. Empty list = no action needed.
    Called by n8n every hour 10 AM–3:30 PM ET.
    """
    from agent.monitor import check_positions

    try:
        alerts = check_positions()
    except Exception as e:
        log.exception("Monitor failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"alerts": alerts, "count": len(alerts)}


@app.post("/positions", dependencies=[Depends(_verify_api_key)])
def set_positions(portfolio: PortfolioState):
    """
    Save the current full portfolio state. Call this after executing trades on StockTrak.
    Overwrites open positions — send the complete current picture each time.
    Include closed_today to record realized P&L for positions just exited.
    """
    from agent import logger as trade_logger

    try:
        if portfolio.closed_today:
            trade_logger.save_closed_positions(portfolio.closed_today)

        portfolio_dict = portfolio.model_dump(exclude={"closed_today"})
        saved = trade_logger.save_portfolio_state(portfolio_dict)
    except Exception as e:
        log.exception("Portfolio save failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "saved", "closed_recorded": len(portfolio.closed_today), **saved}


@app.get("/positions", dependencies=[Depends(_verify_api_key)])
def get_positions():
    """Return the current saved portfolio state."""
    from agent import logger as trade_logger

    return trade_logger.get_portfolio_state()


@app.post("/eod-snapshot", dependencies=[Depends(_verify_api_key)])
def eod_snapshot():
    """
    Build and save an end-of-day portfolio snapshot.
    Fetches live prices for all open positions, calculates unrealized P&L,
    and appends the result to logs/snapshots.json.
    Call this once daily around 4:05 PM ET after the close.
    """
    from agent.eod import build_eod_snapshot

    try:
        snapshot = build_eod_snapshot()
    except Exception as e:
        log.exception("EOD snapshot failed")
        raise HTTPException(status_code=500, detail=str(e))

    return snapshot


@app.get("/closed-positions", dependencies=[Depends(_verify_api_key)])
def closed_positions():
    """Return all closed positions with win/loss summary stats."""
    from agent import logger as trade_logger

    positions = trade_logger.get_closed_positions()
    summary = trade_logger.get_closed_positions_summary(positions)
    return {"closed_positions": positions, "summary": summary}


@app.get("/calibration", dependencies=[Depends(_verify_api_key)])
def calibration():
    """
    Analyze whether stated confidence scores predict actual outcomes.
    Groups closed positions into 50-69, 70-84, and 85-100 confidence buckets.
    """
    from agent import logger as trade_logger

    positions = trade_logger.get_closed_positions()
    return trade_logger.get_calibration_buckets(positions)


@app.get("/weekly-digest", dependencies=[Depends(_verify_api_key)])
def weekly_digest():
    """
    Past-7-day performance summary. Intended for a Friday afternoon n8n workflow.
    Pulls closed trades from trades.json and portfolio value from snapshots.json.
    """
    from agent import logger as trade_logger

    STARTING_VALUE = 50_008.22
    COMPETITION_END = date(2026, 7, 8)

    today = datetime.now(timezone.utc).date()
    week_ago = today - timedelta(days=7)

    # Current value: most recent EOD snapshot
    all_snapshots = trade_logger.get_all_snapshots()
    latest_snapshot = all_snapshots[-1] if all_snapshots else None
    current_value = latest_snapshot.get("estimated_total_value", STARTING_VALUE) if latest_snapshot else STARTING_VALUE

    # Week-ago value: most recent snapshot dated on or before week_ago
    week_ago_str = week_ago.isoformat()
    past_snapshots = [s for s in all_snapshots if s.get("snapshot_date", "") <= week_ago_str]
    week_ago_value = past_snapshots[-1].get("estimated_total_value", STARTING_VALUE) if past_snapshots else STARTING_VALUE

    # Closed positions this week (exit_date within last 7 days)
    all_closed = trade_logger.get_closed_positions()
    closed_this_week = []
    for p in all_closed:
        try:
            exit_date = date.fromisoformat(p.get("exit_date", ""))
            if exit_date >= week_ago:
                closed_this_week.append(p)
        except (ValueError, TypeError):
            pass

    # Trades opened this week — open positions + closed-this-week with entry_date >= week_ago
    portfolio = trade_logger.get_portfolio_state()
    all_open = portfolio.get("open_positions", []) + portfolio.get("open_shorts", [])
    opened_this_week = 0
    for p in all_open + all_closed:
        try:
            entry_date = date.fromisoformat(p.get("entry_date", ""))
            if entry_date >= week_ago:
                opened_this_week += 1
        except (ValueError, TypeError):
            pass

    open_count = len(portfolio.get("open_positions", [])) + len(portfolio.get("open_shorts", []))

    if closed_this_week:
        best = max(closed_this_week, key=lambda p: p.get("realized_pnl_pct", float("-inf")))
        worst = min(closed_this_week, key=lambda p: p.get("realized_pnl_pct", float("inf")))
        best_trade = {"ticker": best.get("ticker"), "realized_pnl_pct": best.get("realized_pnl_pct")}
        worst_trade = {"ticker": worst.get("ticker"), "realized_pnl_pct": worst.get("realized_pnl_pct")}
    else:
        best_trade = None
        worst_trade = None

    total_change = round(current_value - STARTING_VALUE, 2)
    week_change = round(current_value - week_ago_value, 2)

    return {
        "week_ending": today.isoformat(),
        "starting_value": STARTING_VALUE,
        "current_value": round(current_value, 2),
        "week_change_dollars": week_change,
        "week_change_pct": round(week_change / week_ago_value * 100, 2) if week_ago_value else 0.0,
        "total_change_dollars": total_change,
        "total_change_pct": round(total_change / STARTING_VALUE * 100, 2),
        "trades_opened_this_week": opened_this_week,
        "trades_closed_this_week": len(closed_this_week),
        "closed_this_week": closed_this_week,
        "open_positions_count": open_count,
        "best_trade_this_week": best_trade,
        "worst_trade_this_week": worst_trade,
        "days_remaining_in_competition": max(0, (COMPETITION_END - today).days),
    }
