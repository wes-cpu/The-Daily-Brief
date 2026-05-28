"""
futures.py - Fetches CBOT futures prices and technical indicators.

Uses yfinance for continuous front-month prices and ta for RSI/MA.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator

logger = logging.getLogger(__name__)

# Symbol mapping for continuous contracts
CONTINUOUS_SYMBOLS = {
    "corn": "ZC=F",
    "soybeans": "ZS=F",
    "wheat": "ZW=F",
}

# Deferred contract symbols: (commodity, month_code, description)
# We'll dynamically determine the year suffix based on today's date
DEFERRED_CONFIGS = {
    "corn": [
        {"month_code": "U", "month_name": "September"},
        {"month_code": "Z", "month_name": "December"},
    ],
    "soybeans": [
        {"month_code": "Q", "month_name": "August"},
        {"month_code": "X", "month_name": "November"},
    ],
    "wheat": [
        {"month_code": "U", "month_name": "September"},
        {"month_code": "Z", "month_name": "December"},
    ],
}

# Month codes to month numbers for date logic
MONTH_CODE_MAP = {
    "F": 1,   # January
    "G": 2,   # February
    "H": 3,   # March
    "K": 5,   # May
    "M": 6,   # June (not used but common)
    "N": 7,   # July
    "Q": 8,   # August
    "U": 9,   # September
    "V": 10,  # October
    "X": 11,  # November
    "Z": 12,  # December
}

COMMODITY_NAMES = {
    "corn": "Corn",
    "soybeans": "Soybeans",
    "wheat": "Wheat",
}


def _get_deferred_year(month_code: str) -> int:
    """Determine the appropriate contract year for a deferred month code."""
    today = datetime.today()
    contract_month = MONTH_CODE_MAP.get(month_code, 7)
    year = today.year
    # If the contract month has already passed this year, use next year
    if contract_month <= today.month:
        year += 1
    return year


def _build_deferred_ticker(commodity: str, month_code: str) -> tuple[str, str]:
    """Build the yfinance ticker and human-readable name for a deferred contract."""
    ticker_prefix = {
        "corn": "ZC",
        "soybeans": "ZS",
        "wheat": "ZW",
    }[commodity]
    year = _get_deferred_year(month_code)
    year_suffix = str(year)[-2:]  # e.g., "26" for 2026
    ticker = f"{ticker_prefix}{month_code}{year_suffix}=F"
    name = f"{ticker_prefix}{month_code}{year}"
    return ticker, name


def fetch_continuous_price(commodity: str) -> Optional[dict]:
    """
    Fetch front-month continuous contract price and technical indicators.

    Returns dict with:
        symbol, name, price, change, change_pct,
        rsi14, ma14, ma200
    """
    symbol = CONTINUOUS_SYMBOLS[commodity]
    name = COMMODITY_NAMES[commodity]

    try:
        logger.info(f"Fetching {name} continuous contract ({symbol})")
        ticker = yf.Ticker(symbol)

        # Download 1 year for 200-day MA
        end = datetime.today()
        start = end - timedelta(days=400)  # extra buffer for trading days
        hist = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))

        if hist.empty:
            logger.error(f"No history returned for {symbol}")
            return None

        # Sort ascending for TA
        hist = hist.sort_index()

        close = hist["Close"]
        if len(close) < 2:
            logger.error(f"Insufficient history for {symbol}")
            return None

        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        daily_change = latest_close - prev_close
        daily_change_pct = (daily_change / prev_close) * 100 if prev_close != 0 else 0.0

        # RSI (14-period)
        rsi_ind = RSIIndicator(close=close, window=14)
        rsi_series = rsi_ind.rsi()
        rsi14 = float(rsi_series.iloc[-1]) if not rsi_series.empty else None

        # 14-day MA
        sma14_ind = SMAIndicator(close=close, window=14)
        sma14_series = sma14_ind.sma_indicator()
        ma14 = float(sma14_series.iloc[-1]) if not sma14_series.empty else None

        # 200-day MA
        if len(close) >= 200:
            sma200_ind = SMAIndicator(close=close, window=200)
            sma200_series = sma200_ind.sma_indicator()
            ma200 = float(sma200_series.iloc[-1]) if not sma200_series.empty else None
        else:
            logger.warning(f"Only {len(close)} trading days of data for {symbol}; skipping 200-MA")
            ma200 = None

        last_date = hist.index[-1].strftime("%Y-%m-%d")

        return {
            "symbol": symbol,
            "name": name,
            "commodity": commodity,
            "price": latest_close,
            "change": daily_change,
            "change_pct": daily_change_pct,
            "rsi14": rsi14,
            "ma14": ma14,
            "ma200": ma200,
            "as_of_date": last_date,
        }

    except Exception as e:
        logger.error(f"Error fetching continuous price for {symbol}: {e}", exc_info=True)
        return None


def fetch_deferred_contracts(commodity: str, front_month_price: float) -> list[dict]:
    """
    Fetch deferred contract prices and calculate spreads vs front month.

    Returns list of dicts with:
        ticker, name, price, spread (deferred - front_month)
    """
    configs = DEFERRED_CONFIGS.get(commodity, [])
    results = []

    for cfg in configs:
        month_code = cfg["month_code"]
        month_name = cfg["month_name"]
        ticker, contract_name = _build_deferred_ticker(commodity, month_code)

        try:
            logger.info(f"Fetching deferred contract {contract_name} ({ticker})")
            t = yf.Ticker(ticker)
            # Just need recent price
            hist = t.history(period="5d")

            if hist.empty:
                logger.warning(f"No data for deferred contract {ticker}")
                continue

            hist = hist.sort_index()
            price = float(hist["Close"].iloc[-1])
            spread = price - front_month_price

            results.append({
                "ticker": ticker,
                "contract_name": contract_name,
                "month_name": month_name,
                "commodity": commodity,
                "price": price,
                "spread": spread,
            })

        except Exception as e:
            logger.error(f"Error fetching deferred contract {ticker}: {e}", exc_info=True)

    return results


def fetch_all_futures() -> dict:
    """
    Master function: fetch all futures data (front-month + deferred) for
    corn, soybeans, and wheat.

    Returns:
        {
            "front_month": {"corn": {...}, "soybeans": {...}, "wheat": {...}},
            "deferred": {"corn": [...], "soybeans": [...], "wheat": [...]},
        }
    """
    front_month = {}
    deferred = {}

    for commodity in ["corn", "soybeans", "wheat"]:
        data = fetch_continuous_price(commodity)
        front_month[commodity] = data

        if data is not None:
            deferred[commodity] = fetch_deferred_contracts(commodity, data["price"])
        else:
            logger.warning(f"Skipping deferred contracts for {commodity} (no front-month data)")
            deferred[commodity] = []

    return {
        "front_month": front_month,
        "deferred": deferred,
    }
