"""
Persistent trade log. Reads/writes logs/trades.json.
Structure:
  { "analyses": [...], "open_positions": [...], "open_shorts": [...] }
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "trades.json"

logger = logging.getLogger(__name__)

_EMPTY = {"analyses": [], "open_positions": [], "open_shorts": []}


def _read() -> dict:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        return dict(_EMPTY)
    try:
        with open(LOG_PATH) as f:
            data = json.load(f)
        # Ensure all top-level keys exist
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


def get_open_positions() -> tuple[list, list]:
    """Return (open_positions, open_shorts) from the log."""
    data = _read()
    return data.get("open_positions", []), data.get("open_shorts", [])


def update_positions(open_positions: list, open_shorts: list | None = None) -> None:
    """
    Overwrite open positions in the log.
    Call this after manually executing a trade on StockTrak.
    """
    data = _read()
    data["open_positions"] = open_positions
    if open_shorts is not None:
        data["open_shorts"] = open_shorts
    _write(data)
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
