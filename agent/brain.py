"""
Core Claude analysis logic.
Receives a fully assembled data_packet, calls Claude, validates output, returns structured JSON.
"""

import json
import logging
import math
import os

import anthropic
from dotenv import load_dotenv

from agent import logger as trade_logger
from agent.data_fetcher import build_data_packet
from agent.prompts import MODEL, SYSTEM_PROMPT, build_analysis_prompt

load_dotenv()

log = logging.getLogger(__name__)

COMMISSION = 10.0
MIN_TRADE = 1_000.0
MAX_POSITION_PCT = 0.25
MIN_PRICE = 3.0
MIN_CASH_RESERVE_PCT = 0.15

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _correct_long(rec: dict, portfolio_value: float) -> dict | None:
    """
    Re-derive share count and cost from position_size_pct (or a 15% default).
    Returns the corrected rec dict, or None if the trade doesn't meet minimums.
    """
    entry = rec.get("entry_price", 0)
    if entry < MIN_PRICE:
        log.warning("Skipping %s: entry price $%.2f below minimum $%.2f", rec.get("ticker"), entry, MIN_PRICE)
        return None

    # Use Claude's suggested position size, capped at 25%
    raw_pct = min(rec.get("position_size_pct", 15) / 100, MAX_POSITION_PCT)
    position_dollars = raw_pct * portfolio_value

    shares = math.floor((position_dollars - COMMISSION) / entry)
    if shares <= 0:
        return None

    total_cost = shares * entry + COMMISSION
    if total_cost > MAX_POSITION_PCT * portfolio_value:
        shares = math.floor((MAX_POSITION_PCT * portfolio_value - COMMISSION) / entry)
        if shares <= 0:
            return None
        total_cost = shares * entry + COMMISSION

    if total_cost < MIN_TRADE:
        log.warning("Skipping %s: total cost $%.2f below minimum $%.2f", rec.get("ticker"), total_cost, MIN_TRADE)
        return None

    rec["shares"] = shares
    rec["total_cost"] = round(shares * entry, 2)
    rec["commission"] = COMMISSION
    rec["total_with_commission"] = round(total_cost, 2)
    rec["position_size_pct"] = round(shares * entry / portfolio_value * 100, 1)
    return rec


def _correct_short(rec: dict, portfolio_value: float) -> dict | None:
    """Re-derive share count and proceeds for a short recommendation."""
    entry = rec.get("entry_price", 0)
    if entry < MIN_PRICE:
        log.warning("Skipping short %s: entry price $%.2f below minimum", rec.get("ticker"), entry)
        return None

    raw_pct = min(rec.get("position_size_pct", 15) / 100, MAX_POSITION_PCT)
    position_dollars = raw_pct * portfolio_value

    shares = math.floor((position_dollars - COMMISSION) / entry)
    if shares <= 0:
        return None

    # For shorts, the notional value must also not exceed 25% of portfolio
    notional = shares * entry
    if notional > MAX_POSITION_PCT * portfolio_value:
        shares = math.floor(MAX_POSITION_PCT * portfolio_value / entry)
        if shares <= 0:
            return None

    total_proceeds = round(shares * entry - COMMISSION, 2)
    if total_proceeds < MIN_TRADE - COMMISSION * 2:
        return None

    rec["shares"] = shares
    rec["total_proceeds"] = total_proceeds
    rec["commission"] = COMMISSION
    rec["position_size_pct"] = round(shares * entry / portfolio_value * 100, 1)
    return rec


def _enforce_cash_reserve(
    recs: list[dict],
    short_recs: list[dict],
    cash: float,
    portfolio_value: float,
) -> tuple[list[dict], list[dict]]:
    """
    Trim lowest-confidence recommendations until 15% cash reserve is maintained.
    Sorts by confidence descending, drops from bottom first.
    """
    min_cash = MIN_CASH_RESERVE_PCT * portfolio_value
    available = cash

    # Sort by confidence descending (keep highest conviction trades)
    recs_sorted = sorted(recs, key=lambda r: r.get("confidence", 0), reverse=True)
    approved_long: list[dict] = []
    for rec in recs_sorted:
        cost = rec.get("total_with_commission", 0)
        if available - cost >= min_cash:
            approved_long.append(rec)
            available -= cost

    # Shorts consume margin room (simplified: treat as cash reserve risk)
    short_sorted = sorted(short_recs, key=lambda r: r.get("confidence", 0), reverse=True)
    approved_short: list[dict] = []
    for rec in short_sorted:
        notional = rec.get("shares", 0) * rec.get("entry_price", 0)
        if available - notional * 0.25 >= min_cash:  # rough margin requirement
            approved_short.append(rec)

    dropped_long = len(recs) - len(approved_long)
    dropped_short = len(short_recs) - len(approved_short)
    if dropped_long or dropped_short:
        log.info(
            "Cash reserve enforcement: dropped %d long, %d short recommendations",
            dropped_long, dropped_short,
        )

    return approved_long, approved_short


def _validate_output(result: dict, portfolio: dict) -> dict:
    """
    Validate and correct all recommendations in a Claude output dict.
    Modifies in place and returns the corrected dict.
    """
    portfolio_value = portfolio.get("total_value", 50_000.0)
    cash = portfolio.get("cash", portfolio_value)

    corrected_recs = []
    for rec in result.get("recommendations", []):
        fixed = _correct_long(rec, portfolio_value)
        if fixed:
            corrected_recs.append(fixed)

    corrected_shorts = []
    for rec in result.get("short_recommendations", []):
        fixed = _correct_short(rec, portfolio_value)
        if fixed:
            corrected_shorts.append(fixed)

    corrected_recs, corrected_shorts = _enforce_cash_reserve(
        corrected_recs, corrected_shorts, cash, portfolio_value
    )

    result["recommendations"] = corrected_recs
    result["short_recommendations"] = corrected_shorts
    return result


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, retry: bool = False) -> dict:
    """
    Call Claude with the analysis prompt. On retry, appends a stricter instruction.
    Returns parsed dict or raises on second failure.
    """
    client = _get_client()

    if retry:
        prompt = (
            prompt
            + "\n\nCRITICAL: Your previous response was not valid JSON. "
            "Return ONLY the JSON object — no markdown, no backticks, no explanation. "
            "Start your response with { and end with }."
        )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_analysis(data_packet: dict | None = None, triggered_by: str = "manual") -> dict:
    """
    Main entry point for a morning analysis run.

    data_packet: if None, calls build_data_packet() with placeholder portfolio.
    triggered_by: "n8n" or "manual" — stored in the log.

    Returns the validated Claude recommendation dict.
    """
    if data_packet is None:
        data_packet = build_data_packet()

    portfolio = data_packet.get("portfolio", {})
    prompt = build_analysis_prompt(data_packet)

    log.info("Calling Claude (%s) for analysis...", MODEL)
    try:
        result = _call_claude(prompt)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("JSON parse failed on first attempt: %s — retrying with stricter prompt", e)
        try:
            result = _call_claude(prompt, retry=True)
        except Exception as e2:
            log.error("Claude call failed after retry: %s", e2)
            return {"error": str(e2), "status": "failed"}
    except anthropic.APIError as e:
        log.error("Anthropic API error: %s", e)
        return {"error": str(e), "status": "api_error"}

    # Validate and correct all position sizes / commission math
    result = _validate_output(result, portfolio)

    # Persist
    run_id = trade_logger.save_analysis(result, triggered_by=triggered_by)
    result["run_id"] = run_id

    log.info(
        "Analysis complete: %d long, %d short, %d watchlist",
        len(result.get("recommendations", [])),
        len(result.get("short_recommendations", [])),
        len(result.get("watchlist", [])),
    )
    return result


if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run_analysis()
    print(json.dumps(result, indent=2))
