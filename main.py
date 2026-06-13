"""
FastAPI entry point. Exposes /analyze, /monitor, /positions, /health.
All endpoints except /health require X-API-Key header matching AGENT_API_KEY in .env.
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

class AnalyzeRequest(BaseModel):
    triggered_by: str = Field(default="n8n")


class PortfolioState(BaseModel):
    cash: float = Field(..., description="Current cash balance in USD")
    total_value: float = Field(..., description="Total portfolio value in USD")
    open_positions: list[dict] = Field(default_factory=list)
    open_shorts: list[dict] = Field(default_factory=list)


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
    Overwrites the previous state — send the complete current picture each time.
    """
    from agent import logger as trade_logger

    try:
        saved = trade_logger.save_portfolio_state(portfolio.model_dump())
    except Exception as e:
        log.exception("Portfolio save failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "saved", **saved}


@app.get("/positions", dependencies=[Depends(_verify_api_key)])
def get_positions():
    """Return the current saved portfolio state."""
    from agent import logger as trade_logger

    return trade_logger.get_portfolio_state()
