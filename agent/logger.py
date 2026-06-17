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

# Resolve at call time (not import time) so DATA_DIR is always current even if
# the module is imported before Railway finishes injecting environment variables.
import os

_FALLBACK_BASE = Path(__file__).parent.parent / "logs"


def _log_path() -> Path:
    data_dir = os.getenv("DATA_DIR")
    base = Path(data_dir) if data_dir else _FALLBACK_BASE
    base.mkdir(parents=True, exist_ok=True)
    return base / "trades.json"


def _snapshot_path() -> Path:
    return _log_path().parent / "snapshots.json"


logger = logging.getLogger(__name__)

_EMPTY = {"analyses": [], "portfolio": None, "closed_positions": []}

_DEFAULT_PORTFOLIO = {
    "cash": 50_008.22,
    "total_value": 50_008.22,
    "open_positions": [],
    "open_shorts": [],
}


def _read() -> dict:
    path = _log_path()
    if not path.exists():
        logger.debug("trades.json not found at %s — returning empty state", path)
        return dict(_EMPTY)
    try:
        with open(path) as f:
            data = json.load(f)
        for k, v in _EMPTY.items():
            data.setdefault(k, v)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Could not read trades.json at %s: %s", path, e)
        return dict(_EMPTY)


def _write(data: dict) -> None:
    path = _log_path()
    with open(path, "w") as f:
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
    path = _log_path()
    logger.debug("Loading portfolio from %s", path)
    data = _read()
    portfolio = data.get("portfolio")
    if portfolio is None:
        logger.warning("No portfolio saved at %s — returning default starting state", path)
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
# Closed position log (stored inside trades.json under "closed_positions")
# ---------------------------------------------------------------------------

_COMMISSION = 20.0  # $10 per leg, both sides


def _calc_pnl(direction: str, entry: float, exit_price: float, shares: int) -> tuple[float, float]:
    """Return (realized_pnl_dollars, realized_pnl_pct) for a closed position."""
    if direction == "SHORT":
        dollars = round((entry - exit_price) * shares - _COMMISSION, 2)
        pct = round((entry - exit_price) / entry * 100, 2) if entry else 0.0
    else:
        dollars = round((exit_price - entry) * shares - _COMMISSION, 2)
        pct = round((exit_price - entry) / entry * 100, 2) if entry else 0.0
    return dollars, pct


def save_closed_positions(closed_list: list[dict]) -> None:
    """
    Validate and append closed positions to trades.json.
    Recalculates realized_pnl_dollars server-side; warns if provided value differs by > $0.01.
    """
    if not closed_list:
        return
    data = _read()
    data.setdefault("closed_positions", [])
    timestamp = datetime.now(timezone.utc).isoformat()

    for pos in closed_list:
        entry = pos.get("entry_price", 0.0)
        exit_price = pos.get("exit_price", 0.0)
        shares = pos.get("shares", 0)
        direction = pos.get("direction", "LONG").upper()

        server_pnl, server_pct = _calc_pnl(direction, entry, exit_price, shares)

        provided_pnl = pos.get("realized_pnl_dollars")
        if provided_pnl is not None and abs(provided_pnl - server_pnl) > 0.01:
            logger.warning(
                "P&L mismatch for %s: provided=%.2f, server-calculated=%.2f (using server value)",
                pos.get("ticker"), provided_pnl, server_pnl,
            )

        pos["realized_pnl_dollars"] = server_pnl
        pos["realized_pnl_pct"] = server_pct
        pos.setdefault("recorded_at", timestamp)

    data["closed_positions"].extend(closed_list)
    _write(data)
    logger.info("Saved %d closed position(s)", len(closed_list))


def get_closed_positions() -> list[dict]:
    """Return all closed positions."""
    data = _read()
    return data.get("closed_positions", [])


def get_closed_positions_summary(positions: list[dict] | None = None) -> dict:
    """Return win/loss summary stats over a list of closed positions."""
    if positions is None:
        positions = get_closed_positions()

    if not positions:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": None,
            "total_realized_pnl": 0.0,
            "avg_win_dollars": None,
            "avg_loss_dollars": None,
            "avg_days_held": None,
        }

    wins = [p for p in positions if p.get("realized_pnl_dollars", 0) > 0]
    losses = [p for p in positions if p.get("realized_pnl_dollars", 0) <= 0]
    total_pnl = sum(p.get("realized_pnl_dollars", 0) for p in positions)

    avg_win = round(sum(p["realized_pnl_dollars"] for p in wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(p["realized_pnl_dollars"] for p in losses) / len(losses), 2) if losses else None

    days_list = [p["actual_days_held"] for p in positions if p.get("actual_days_held") is not None]
    avg_days = round(sum(days_list) / len(days_list), 1) if days_list else None

    return {
        "total_trades": len(positions),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(positions) * 100, 1),
        "total_realized_pnl": round(total_pnl, 2),
        "avg_win_dollars": avg_win,
        "avg_loss_dollars": avg_loss,
        "avg_days_held": avg_days,
    }


def get_calibration_buckets(positions: list[dict] | None = None) -> dict:
    """Group closed positions by confidence bucket and compute win rates."""
    if positions is None:
        positions = get_closed_positions()

    _BUCKETS = [("85-100", 85, 100), ("70-84", 70, 84), ("50-69", 50, 69)]
    buckets = []

    for label, low, high in _BUCKETS:
        subset = [p for p in positions if low <= p.get("original_confidence", -1) <= high]
        if not subset:
            buckets.append({"confidence_range": label, "count": 0, "win_rate_pct": None, "avg_pnl_pct": None})
        else:
            wins = sum(1 for p in subset if p.get("realized_pnl_dollars", 0) > 0)
            avg_pnl = round(sum(p.get("realized_pnl_pct", 0) for p in subset) / len(subset), 1)
            buckets.append({
                "confidence_range": label,
                "count": len(subset),
                "win_rate_pct": round(wins / len(subset) * 100, 1),
                "avg_pnl_pct": avg_pnl,
            })

    total = len(positions)
    if total < 3:
        note = f"Insufficient data — only {total} closed trade{'s' if total != 1 else ''} so far."
    else:
        filled = [b for b in buckets if b["count"] > 0]
        if len(filled) >= 2:
            top = filled[0]
            bot = filled[-1]
            note = (
                f"{top['confidence_range']}% confidence picks achieved {top['win_rate_pct']}% win rate "
                f"vs {bot['win_rate_pct']}% for the {bot['confidence_range']}% range "
                f"({total} total trades)."
            )
        else:
            note = f"All {total} trades fall in one confidence bucket — no cross-bucket comparison yet."

    return {"buckets": buckets, "overall_correlation_note": note}


# ---------------------------------------------------------------------------
# Snapshot log (snapshots.json, same directory as trades.json)
# ---------------------------------------------------------------------------


def get_previous_snapshot() -> dict | None:
    """Return the most recent EOD snapshot, or None if none saved yet."""
    path = _snapshot_path()
    if not path.exists():
        return None
    try:
        with open(path) as f:
            snapshots = json.load(f)
        return snapshots[-1] if snapshots else None
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Could not read snapshots.json at %s: %s", path, e)
        return None


def save_snapshot(snapshot: dict) -> None:
    """Append an EOD snapshot to snapshots.json."""
    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshots: list = []
    if path.exists():
        try:
            with open(path) as f:
                snapshots = json.load(f)
        except (json.JSONDecodeError, OSError):
            snapshots = []
    snapshots.append(snapshot)
    with open(path, "w") as f:
        json.dump(snapshots, f, indent=2, default=str)
    logger.info(
        "Snapshot saved: date=%s, estimated_value=%.2f",
        snapshot.get("snapshot_date"),
        snapshot.get("estimated_total_value", 0),
    )


def get_all_snapshots() -> list[dict]:
    """Return all EOD snapshots in chronological order."""
    path = _snapshot_path()
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Could not read snapshots.json at %s: %s", path, e)
        return []
