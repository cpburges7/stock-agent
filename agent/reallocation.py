"""
reallocation.py
---------------
Swap-recommendation logic for a fully-invested portfolio.

When you have little/no cash but the agent finds a high-conviction new idea,
this surfaces "sell your weakest holding X to fund new idea Y" — but ONLY when
the conviction edge is large enough to plausibly clear the $20 round-trip
commission drag. It shows the math so you decide per-swap.

This module is pure logic (no API calls). brain.py calls evaluate_swaps()
after Claude returns its recommendations, and attaches the result to the output.
"""

import logging

log = logging.getLogger(__name__)

# ---- Tunable knobs -------------------------------------------------------
# Conviction-point gap required before a swap is even surfaced.
# You chose AGGRESSIVE (5). Raise to 10 (moderate) or 15 (conservative) if it
# fires too often and churns you. This is the single number to edit.
SWAP_CONVICTION_GAP = 5

# Round-trip commission to escape on a swap: $10 to sell the old + $10 to buy
# the new = $20. Used only to show the breakeven math, not to block swaps.
ROUND_TRIP_COMMISSION = 20.0

# Don't bother surfacing a swap if the freed-up position is tiny — the dollars
# moved won't matter and the commission drag dominates.
MIN_SWAP_POSITION_VALUE = 2000.0


def _position_value(pos: dict) -> float:
    """Market value of a held position from current price * shares."""
    shares = abs(pos.get("shares", 0))
    price = pos.get("current_price") or pos.get("entry_price") or 0
    return shares * price


def _commission_drag_pct(position_value: float) -> float:
    """What % gain the NEW position must clear just to cover the $20 round trip."""
    if position_value <= 0:
        return 0.0
    return ROUND_TRIP_COMMISSION / position_value * 100


def evaluate_swaps(
    held_conviction: list[dict],
    new_candidates: list[dict],
    gap_threshold: int = SWAP_CONVICTION_GAP,
) -> list[dict]:
    """
    Compare held positions against unheld new candidates and surface swaps.

    held_conviction: list of {ticker, conviction, position_value} for CURRENT holdings,
                     where conviction is the agent's fresh 0-100 score for that name today.
    new_candidates:  list of {ticker, confidence} for high-conviction ideas the agent
                     likes that you do NOT currently hold.

    Returns a list of swap suggestion dicts, best edge first. Empty list if none qualify.
    Each suggestion includes the commission breakeven so the human can judge.
    """
    if not held_conviction or not new_candidates:
        return []

    # Weakest holdings first — these are the swap-out candidates.
    held_sorted = sorted(held_conviction, key=lambda h: h.get("conviction", 100))
    # Strongest new ideas first — these are the swap-in candidates.
    new_sorted = sorted(new_candidates, key=lambda n: n.get("confidence", 0), reverse=True)

    suggestions = []
    used_new = set()

    for held in held_sorted:
        held_conv = held.get("conviction", 100)
        held_val = held.get("position_value", 0)

        if held_val < MIN_SWAP_POSITION_VALUE:
            continue  # too small to bother swapping

        # Find the best new idea not already paired off
        for cand in new_sorted:
            if cand["ticker"] in used_new:
                continue
            edge = cand.get("confidence", 0) - held_conv
            if edge >= gap_threshold:
                drag = _commission_drag_pct(held_val)
                suggestions.append({
                    "sell": held["ticker"],
                    "sell_conviction": held_conv,
                    "buy": cand["ticker"],
                    "buy_confidence": cand.get("confidence", 0),
                    "conviction_edge": edge,
                    "freed_capital": round(held_val, 2),
                    "commission_breakeven_pct": round(drag, 2),
                    "note": (
                        f"Selling {held['ticker']} (conv {held_conv}) to buy "
                        f"{cand['ticker']} (conf {cand.get('confidence',0)}) = +{edge} edge. "
                        f"New pick must gain >{round(drag,2)}% just to cover the $20 "
                        f"round-trip. Only swap if you believe the edge exceeds that."
                    ),
                })
                used_new.add(cand["ticker"])
                break  # one swap per held position

    return suggestions


def build_held_conviction(open_positions: list[dict],
                          open_shorts: list[dict],
                          conviction_map: dict[str, int]) -> list[dict]:
    """
    Merge synced holdings with the agent's fresh conviction scores for those tickers.

    conviction_map: {ticker: conviction_0_100} produced by re-scoring held names
                    in today's analysis run (see prompts.py held-ranking instruction).
    Tickers with no fresh score default to conviction 50 (neutral/unknown).
    """
    out = []
    for pos in (open_positions or []) + (open_shorts or []):
        ticker = pos.get("symbol") or pos.get("ticker")
        if not ticker:
            log.warning("Skipping position with no symbol/ticker key: %s", pos)
            continue
        out.append({
            "ticker": ticker,
            "conviction": conviction_map.get(ticker, 50),
            "position_value": _position_value(pos),
        })
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    held = [
        {"ticker": "SOFI", "conviction": 58, "position_value": 9986},
        {"ticker": "GS",   "conviction": 78, "position_value": 11837},
        {"ticker": "AMD",  "conviction": 82, "position_value": 11492},
    ]
    new = [
        {"ticker": "QBTS", "confidence": 74},
        {"ticker": "MRVL", "confidence": 80},
    ]
    for s in evaluate_swaps(held, new):
        print(s["note"])
        print(f"  freed ${s['freed_capital']}, breakeven {s['commission_breakeven_pct']}%\n")