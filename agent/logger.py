"""
Persistent trade log. Reads/writes logs/trades.json.
Structure:
  { "portfolio": {...}, "analyses": [...] }
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "trades.json"

logger = logging.getLogger(__name__)

_EMPTY = {"analyses": [], "portfolio": None}

_DEFAULT_PORTFOLIO = {
    "cash": 50_008.22,
    "total_value": 50_008.22,
    "open_positions": [],
    "open_shorts": [],
}


def _read() -> dict:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        return dict(_EMPTY)
    try:
        with open(LOG_PATH) as f:
            data = json.load(f)
        for k, v in _EMPTY.items():
            data.setdefault(k, v)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Could not read trades.json: %s", e)
        return dict(_EMPTY)


def _write(data: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


def save_analysis(result: dict, triggered_by: str = "manual") -> str:
    """Append a full Claude analysis result to the analyses list. Returns the run_id."""
    data = _read()
    run_id = str(uuid.uuid4())
    data["analyses"].append({
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "triggered_by": triggered_by,
        "result": result,
    })
    _write(data)
    logger.info("Analysis saved: run_id=%s", run_id)
    return run_id


def get_portfolio_state() -> dict:
    """Return the current portfolio state, or the default starting state if none saved."""
    data = _read()
    portfolio = data.get("portfolio")
    if portfolio is None:
        return dict(_DEFAULT_PORTFOLIO)
    return portfolio


def save_portfolio_state(portfolio_dict: dict) -> dict:
    """
    Overwrite the portfolio section of trades.json with the provided state.
    Adds a last_updated timestamp. Returns the saved portfolio object.
    """
    data = _read()
    portfolio = dict(portfolio_dict)
    portfolio["last_updated"] = datetime.now(timezone.utc).isoformat()
    data["portfolio"] = portfolio
    _write(data)
    logger.info(
        "Portfolio saved: cash=%.2f, total=%.2f, %d long, %d short",
        portfolio.get("cash", 0),
        portfolio.get("total_value", 0),
        len(portfolio.get("open_positions", [])),
        len(portfolio.get("open_shorts", [])),
    )
    return portfolio


def get_open_positions() -> tuple[list, list]:
    """Return (open_positions, open_shorts) from the current portfolio state."""
    portfolio = get_portfolio_state()
    return portfolio.get("open_positions", []), portfolio.get("open_shorts", [])


def update_positions(open_positions: list, open_shorts: list | None = None) -> None:
    """Overwrite open positions in the log (legacy — prefer save_portfolio_state)."""
    portfolio = get_portfolio_state()
    portfolio["open_positions"] = open_positions
    if open_shorts is not None:
        portfolio["open_shorts"] = open_shorts
    save_portfolio_state(portfolio)
    logger.info(
        "Positions updated: %d long, %d short",
        len(open_positions),
        len(open_shorts or []),
    )


def get_latest_analysis() -> dict | None:
    """Return the most recent analysis result, or None if none exist."""
    data = _read()
    if not data["analyses"]:
        return None
    return data["analyses"][-1]


# ---------------------------------------------------------------------------
# Snapshot log (logs/snapshots.json)
# ---------------------------------------------------------------------------

SNAPSHOT_PATH = LOG_PATH.parent / "snapshots.json"


def get_previous_snapshot() -> dict | None:
    """Return the most recent EOD snapshot, or None if none saved yet."""
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        with open(SNAPSHOT_PATH) as f:
            snapshots = json.load(f)
        return snapshots[-1] if snapshots else None
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Could not read snapshots.json: %s", e)
        return None


def save_snapshot(snapshot: dict) -> None:
    """Append an EOD snapshot to snapshots.json."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshots: list = []
    if SNAPSHOT_PATH.exists():
        try:
            with open(SNAPSHOT_PATH) as f:
                snapshots = json.load(f)
        except (json.JSONDecodeError, OSError):
            snapshots = []
    snapshots.append(snapshot)
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshots, f, indent=2, default=str)
    logger.info(
        "Snapshot saved: date=%s, estimated_value=%.2f",
        snapshot.get("snapshot_date"),
        snapshot.get("estimated_total_value", 0),
    )
