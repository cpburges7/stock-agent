"""
FastAPI entry point. Exposes /analyze, /monitor, /health.
All endpoints require X-API-Key header matching AGENT_API_KEY in .env.
"""

import logging
import os
from datetime import datetime, timezone

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

class Portfolio(BaseModel):
    cash: float = Field(..., description="Current cash balance in USD")
    total_value: float = Field(..., description="Total portfolio value in USD")
    open_positions: list[dict] = Field(default_factory=list)
    open_shorts: list[dict] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    portfolio: Portfolio
    triggered_by: str = Field(default="n8n")


class Position(BaseModel):
    ticker: str
    direction: str = Field(default="LONG", description="LONG or SHORT")
    entry_price: float
    shares: int
    target_price: float | None = None
    cover_target: float | None = None
    stop_loss: float
    time_horizon_days: int = 5
    opened_date: str | None = None
    is_day_trade: bool = False


class PositionsUpdate(BaseModel):
    open_positions: list[Position] = Field(default_factory=list)
    open_shorts: list[Position] = Field(default_factory=list)


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
    """
    from agent.brain import run_analysis
    from agent.data_fetcher import build_data_packet

    portfolio_dict = request.portfolio.model_dump()

    try:
        data_packet = build_data_packet(portfolio_override=portfolio_dict)
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


@app.patch("/positions", dependencies=[Depends(_verify_api_key)])
def update_positions(request: PositionsUpdate):
    """
    Update open positions after manually executing trades on StockTrak.
    Call this after every trade so the monitor has accurate state.
    """
    from agent import logger as trade_logger

    longs = [p.model_dump() for p in request.open_positions]
    shorts = [p.model_dump() for p in request.open_shorts]

    try:
        trade_logger.update_positions(longs, shorts)
    except Exception as e:
        log.exception("Position update failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "status": "updated",
        "open_positions": len(longs),
        "open_shorts": len(shorts),
    }
