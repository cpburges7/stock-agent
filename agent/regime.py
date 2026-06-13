"""
Market regime classifier. Fetches VIX, SPY/QQQ moving averages, and sector ETF daily
performance to produce a risk-on/risk-off snapshot. No Claude calls — fast and free.
"""

import logging

import yfinance as yf

log = logging.getLogger(__name__)

SECTORS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
}

_RISK_ON_SECTORS = {"Technology", "Consumer Discretionary", "Financials", "Energy"}
_RISK_OFF_SECTORS = {"Utilities", "Consumer Staples", "Healthcare"}

_TREND_LABELS = {
    "above_20ma_above_50ma": "above 20MA and 50MA (strong uptrend)",
    "above_20ma_below_50ma": "above 20MA, below 50MA (short-term bounce)",
    "below_20ma_above_50ma": "below 20MA, above 50MA (pullback in uptrend)",
    "below_20ma_below_50ma": "below 20MA and 50MA (downtrend)",
}


def _vix_signal(vix: float) -> str:
    if vix < 15:
        return "low_volatility"
    if vix < 20:
        return "normal"
    if vix < 30:
        return "elevated"
    return "high_volatility"


def _ma_trend(series) -> str | None:
    """Return trend string from a price series. Needs >= 50 rows."""
    if len(series) < 50:
        return None
    price = float(series.iloc[-1])
    ma20 = float(series.iloc[-20:].mean())
    ma50 = float(series.iloc[-50:].mean())
    above20 = price > ma20
    above50 = price > ma50
    if above20 and above50:
        return "above_20ma_above_50ma"
    if above20:
        return "above_20ma_below_50ma"
    if above50:
        return "below_20ma_above_50ma"
    return "below_20ma_below_50ma"


def _classify_regime(vix: float | None, leading: list[str]) -> str:
    risk_on = sum(1 for s in leading if s in _RISK_ON_SECTORS)
    risk_off = sum(1 for s in leading if s in _RISK_OFF_SECTORS)
    vix_high = vix is not None and vix > 20
    if vix_high or risk_off >= 2:
        return "risk_off"
    if (vix is None or vix < 20) and risk_on >= 2:
        return "risk_on"
    return "mixed"


def _build_summary(
    vix: float | None,
    vix_sig: str | None,
    spy_trend: str | None,
    qqq_trend: str | None,
    regime: str,
    leading: list[str],
    lagging: list[str],
) -> str:
    regime_label = {"risk_on": "Risk-on", "risk_off": "Risk-off", "mixed": "Mixed"}.get(regime, "Mixed")
    _vix_words = {
        "low_volatility": "low volatility",
        "normal": "normal volatility",
        "elevated": "elevated volatility",
        "high_volatility": "high fear/volatility",
    }
    sentences = []

    # Sentence 1: regime + VIX
    s1 = regime_label + " environment"
    if vix is not None and vix_sig:
        s1 += f" with {_vix_words.get(vix_sig, vix_sig)} (VIX {vix:.1f})"
    sentences.append(s1 + ".")

    # Sentence 2: SPY / QQQ
    if spy_trend and qqq_trend:
        if spy_trend == qqq_trend:
            sentences.append(f"SPY and QQQ both {_TREND_LABELS[spy_trend]}.")
        else:
            sentences.append(
                f"SPY {_TREND_LABELS[spy_trend]}; QQQ {_TREND_LABELS[qqq_trend]}."
            )

    # Sentence 3: sector rotation
    if leading or lagging:
        lead_str = ", ".join(leading[:3]) if leading else "none"
        lag_str = ", ".join(lagging[-3:])
        rotation = {"risk_on": "classic risk-on rotation", "risk_off": "defensive rotation"}.get(
            regime, "mixed signals"
        )
        sentences.append(f"{lead_str} leading; {lag_str} lagging — {rotation}.")

    return " ".join(sentences)


def _empty_regime() -> dict:
    return {
        "vix_level": None,
        "vix_signal": None,
        "spy_trend": None,
        "qqq_trend": None,
        "market_regime": "mixed",
        "sector_performance": [],
        "leading_sectors": [],
        "lagging_sectors": [],
        "regime_summary": "Market regime data unavailable — rely on individual ticker technicals.",
    }


def get_market_regime() -> dict:
    """
    Fetch VIX, SPY/QQQ MAs, and all 11 sector ETF daily changes in one batch download.
    Returns regime classification dict. Individual fetch failures degrade gracefully.
    """
    all_tickers = list(SECTORS.keys()) + ["SPY", "QQQ", "^VIX"]

    try:
        raw = yf.download(
            all_tickers,
            period="3mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        closes = raw.get("Close") if hasattr(raw, "get") else raw["Close"]
        if closes is None or closes.empty:
            log.error("Regime: empty data from yfinance")
            return _empty_regime()
        if not hasattr(closes, "columns"):
            closes = closes.to_frame(name=all_tickers[0])
    except Exception as e:
        log.error("Regime: download failed: %s", e)
        return _empty_regime()

    # VIX
    vix_level: float | None = None
    try:
        if "^VIX" in closes.columns:
            vix_s = closes["^VIX"].dropna()
            if not vix_s.empty:
                vix_level = round(float(vix_s.iloc[-1]), 2)
    except Exception:
        pass
    vix_sig = _vix_signal(vix_level) if vix_level is not None else None

    # SPY / QQQ trends (need 50 days of data)
    spy_trend: str | None = None
    qqq_trend: str | None = None
    for ticker, target in [("SPY", "spy_trend"), ("QQQ", "qqq_trend")]:
        try:
            if ticker in closes.columns:
                s = closes[ticker].dropna()
                result = _ma_trend(s)
                if target == "spy_trend":
                    spy_trend = result
                else:
                    qqq_trend = result
        except Exception:
            pass

    # Sector daily % changes
    sector_rows = []
    for etf, sector_name in SECTORS.items():
        try:
            if etf not in closes.columns:
                continue
            s = closes[etf].dropna()
            if len(s) < 2:
                continue
            prev, curr = float(s.iloc[-2]), float(s.iloc[-1])
            change_pct = round((curr - prev) / prev * 100, 2) if prev else None
            if change_pct is not None:
                sector_rows.append({"sector": sector_name, "etf": etf, "change_pct": change_pct})
        except Exception:
            pass

    sector_rows.sort(key=lambda r: r["change_pct"], reverse=True)
    for i, row in enumerate(sector_rows):
        row["rank"] = i + 1

    leading = [r["sector"] for r in sector_rows[:3]]
    lagging = [r["sector"] for r in sector_rows[-3:]]
    regime = _classify_regime(vix_level, leading)
    summary = _build_summary(vix_level, vix_sig, spy_trend, qqq_trend, regime, leading, lagging)

    log.info(
        "Regime: %s | VIX %.1f | SPY %s | leading: %s",
        regime,
        vix_level or 0,
        spy_trend or "N/A",
        ", ".join(leading),
    )

    return {
        "vix_level": vix_level,
        "vix_signal": vix_sig,
        "spy_trend": spy_trend,
        "qqq_trend": qqq_trend,
        "market_regime": regime,
        "sector_performance": sector_rows,
        "leading_sectors": leading,
        "lagging_sectors": lagging,
        "regime_summary": summary,
    }


if __name__ == "__main__":
    import json
    import logging as _logging
    from dotenv import load_dotenv
    load_dotenv()
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(get_market_regime(), indent=2))
