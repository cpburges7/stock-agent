"""
Technical indicator calculations using pandas-ta.
Input:  list of ticker symbols
Output: dict of {ticker: {indicator_name: value, ...}}
"""

import logging

import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = "1y"  # enough for 52-week stats and all indicators


def _detect_macd_signal(macd: pd.Series, signal: pd.Series, lookback: int = 3) -> str:
    """Return human-readable MACD crossover description."""
    for i in range(1, lookback + 1):
        if len(macd) < i + 2:
            break
        prev_diff = macd.iloc[-(i + 1)] - signal.iloc[-(i + 1)]
        curr_diff = macd.iloc[-i] - signal.iloc[-i]
        if prev_diff < 0 and curr_diff >= 0:
            return f"bullish crossover {i}d ago"
        if prev_diff > 0 and curr_diff <= 0:
            return f"bearish crossover {i}d ago"
    return "bullish" if macd.iloc[-1] > signal.iloc[-1] else "bearish"


def _detect_bb_signal(close: pd.Series, bb_upper: pd.Series, bb_lower: pd.Series, bb_pct: pd.Series) -> str:
    """Return Bollinger Band context string."""
    price = close.iloc[-1]
    upper = bb_upper.iloc[-1]
    lower = bb_lower.iloc[-1]

    if price >= upper:
        return "above upper band (overbought)"
    if price <= lower:
        return "below lower band (oversold)"

    # Detect squeeze: bandwidth narrowing over last 5 days vs prior 5
    if len(bb_pct) >= 11:
        recent_bw = bb_pct.iloc[-5:].mean()
        prior_bw = bb_pct.iloc[-11:-5].mean()
        if prior_bw > 0 and recent_bw < prior_bw * 0.85:
            return "squeeze forming"

    pct = bb_pct.iloc[-1] if not np.isnan(bb_pct.iloc[-1]) else 0.5
    if pct > 0.8:
        return "near upper band"
    if pct < 0.2:
        return "near lower band"
    return ""


def _compute_ticker(df: pd.DataFrame) -> dict:
    """Compute all indicators for a single ticker's OHLCV DataFrame."""
    close = df["Close"].dropna()
    high = df["High"].dropna()
    low = df["Low"].dropna()
    volume = df["Volume"].dropna()

    if len(close) < 52:
        return {}

    result: dict = {}

    # Price and daily change
    result["price"] = round(float(close.iloc[-1]), 2)
    result["change_pct"] = round(float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100), 2)

    # RSI
    rsi_series = ta.rsi(close, length=14)
    result["rsi"] = round(float(rsi_series.iloc[-1]), 1) if rsi_series is not None and not rsi_series.empty else None

    # SMA 20 and 50
    sma20 = ta.sma(close, length=20)
    sma50 = ta.sma(close, length=50)
    result["sma20"] = round(float(sma20.iloc[-1]), 2) if sma20 is not None and not sma20.empty else None
    result["sma50"] = round(float(sma50.iloc[-1]), 2) if sma50 is not None and not sma50.empty else None

    # MACD
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is not None and not macd_df.empty:
        macd_col = next((c for c in macd_df.columns if c.startswith("MACD_")), None)
        sig_col = next((c for c in macd_df.columns if c.startswith("MACDs_")), None)
        if macd_col and sig_col:
            result["macd_signal"] = _detect_macd_signal(macd_df[macd_col], macd_df[sig_col])
        else:
            result["macd_signal"] = "N/A"
    else:
        result["macd_signal"] = "N/A"

    # Bollinger Bands
    bb_df = ta.bbands(close, length=20, std=2)
    if bb_df is not None and not bb_df.empty:
        upper_col = next((c for c in bb_df.columns if c.startswith("BBU_")), None)
        lower_col = next((c for c in bb_df.columns if c.startswith("BBL_")), None)
        pct_col = next((c for c in bb_df.columns if c.startswith("BBP_")), None)
        if upper_col and lower_col and pct_col:
            result["bb_signal"] = _detect_bb_signal(close, bb_df[upper_col], bb_df[lower_col], bb_df[pct_col])
        else:
            result["bb_signal"] = ""
    else:
        result["bb_signal"] = ""

    # Volume ratio (today vs 20-day avg)
    if len(volume) >= 21:
        avg_vol = float(volume.iloc[-21:-1].mean())
        today_vol = float(volume.iloc[-1])
        result["volume_ratio"] = round(today_vol / avg_vol, 2) if avg_vol > 0 else None
    else:
        result["volume_ratio"] = None

    # ATR (14-period)
    if len(high) >= 15 and len(low) >= 15:
        atr_series = ta.atr(high, low, close, length=14)
        result["atr"] = round(float(atr_series.iloc[-1]), 2) if atr_series is not None and not atr_series.empty else None
    else:
        result["atr"] = None

    # 52-week high/low position
    high_52 = float(close.tail(252).max())
    low_52 = float(close.tail(252).min())
    price = result["price"]
    result["pct_from_52w_high"] = round((price - high_52) / high_52 * 100, 1)
    result["pct_from_52w_low"] = round((price - low_52) / low_52 * 100, 1)

    # 5-day momentum
    if len(close) >= 6:
        result["momentum_5d"] = round(float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100), 2)
    else:
        result["momentum_5d"] = None

    return result


def get_technicals(tickers: list[str]) -> dict[str, dict]:
    """
    Fetch OHLCV data and compute all technical indicators for the given tickers.
    Returns {ticker: indicators_dict}. Missing or errored tickers are omitted.
    """
    if not tickers:
        return {}

    results: dict[str, dict] = {}

    # Batch download — faster than one-by-one
    try:
        raw = yf.download(
            tickers,
            period=LOOKBACK_DAYS,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
            group_by="ticker",
        )
    except Exception as e:
        logger.error("yfinance batch download failed: %s", e)
        return {}

    single_ticker = len(tickers) == 1

    for ticker in tickers:
        try:
            if single_ticker:
                df = raw.copy()
            else:
                if ticker not in raw.columns.get_level_values(0):
                    continue
                df = raw[ticker].copy()

            df = df.dropna(how="all")
            if df.empty or len(df) < 52:
                logger.debug("Insufficient data for %s (%d rows)", ticker, len(df))
                continue

            indicators = _compute_ticker(df)
            if indicators:
                results[ticker] = indicators
        except Exception as e:
            logger.warning("Technical calc failed for %s: %s", ticker, e)

    logger.info("Technicals computed for %d/%d tickers", len(results), len(tickers))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sample = ["NVDA", "AAPL", "SPY"]
    data = get_technicals(sample)
    for ticker, indicators in data.items():
        print(f"\n{ticker}:")
        for k, v in indicators.items():
            print(f"  {k}: {v}")
