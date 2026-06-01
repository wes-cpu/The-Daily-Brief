"""
futures.py - Fetches CBOT futures prices and technical indicators.

Uses yfinance for continuous front-month prices; RSI and SMA computed
with pure pandas/numpy (no C-extension dependency).
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Symbol mapping for continuous contracts
CONTINUOUS_SYMBOLS = {
    "corn": "ZC=F",
    "soybeans": "ZS=F",
    "wheat": "ZW=F",
}

# All listed contract months per commodity (code, name, calendar-month-number)
# Used to dynamically select genuinely deferred contracts (not the front month).
ALL_CONTRACT_MONTHS: dict[str, list[tuple[str, str, int]]] = {
    "corn":     [("H","March",3),("K","May",5),("N","July",7),("U","September",9),("Z","December",12)],
    "soybeans": [("F","January",1),("H","March",3),("K","May",5),("N","July",7),
                 ("Q","August",8),("U","September",9),("X","November",11)],
    "wheat":    [("H","March",3),("K","May",5),("N","July",7),("U","September",9),("Z","December",12)],
}

# Month codes to month numbers for date logic
MONTH_CODE_MAP = {
    "F": 1,   # January
    "G": 2,   # February
    "H": 3,   # March
    "K": 5,   # May
    "M": 6,   # June
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

TICKER_PREFIX = {"corn": "ZC", "soybeans": "ZS", "wheat": "ZW"}


def _select_deferred_contracts(commodity: str, n: int = 2) -> list[dict]:
    """
    Return the n nearest contract months that are genuinely deferred
    (at least 2 calendar months after today).  This avoids returning the
    front-month contract as a "deferred" position (e.g., picking July when
    July IS the current front month in June).
    """
    today = datetime.today()
    threshold_month = today.month + 2  # need at least 2 months out
    threshold_year = today.year
    if threshold_month > 12:
        threshold_month -= 12
        threshold_year += 1

    prefix = TICKER_PREFIX[commodity]
    candidates: list[dict] = []

    for year in [today.year, today.year + 1, today.year + 2]:
        for code, name, month_num in ALL_CONTRACT_MONTHS[commodity]:
            sort_key = year * 100 + month_num
            ref_key = threshold_year * 100 + threshold_month
            if sort_key < ref_key:
                continue
            year_suffix = str(year)[-2:]
            # Yahoo Finance accepts two formats for dated CBOT futures:
            #   ZCN26=F  (short year, =F suffix)  ← sometimes 404s
            #   ZCN26.CBT (CBOT exchange suffix)   ← more reliable
            # We try both; fetch_deferred_contracts tries the list in order.
            candidates.append({
                "month_code": code,
                "month_name": f"{name} {year}",
                "tickers": [
                    f"{prefix}{code}{year_suffix}.CBT",
                    f"{prefix}{code}{year_suffix}=F",
                ],
                "contract_name": f"{prefix}{code}{year}",
                "commodity": commodity,
                "sort_key": sort_key,
            })

    candidates.sort(key=lambda x: x["sort_key"])
    return candidates[:n]


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Compute RSI using Wilder's smoothed moving average."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window=window, min_periods=window).mean()


def fetch_continuous_price(commodity: str) -> Optional[dict]:
    """
    Fetch front-month continuous contract price and technical indicators.

    Returns dict with: symbol, name, price, change, change_pct,
        rsi14, ma14, ma200, as_of_date
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
        rsi_series = _rsi(close, window=14)
        rsi14 = float(rsi_series.iloc[-1]) if not rsi_series.empty and pd.notna(rsi_series.iloc[-1]) else None

        # 14-day MA
        sma14_series = _sma(close, window=14)
        ma14 = float(sma14_series.iloc[-1]) if not sma14_series.empty and pd.notna(sma14_series.iloc[-1]) else None

        # 200-day MA
        if len(close) >= 200:
            sma200_series = _sma(close, window=200)
            ma200 = float(sma200_series.iloc[-1]) if not sma200_series.empty and pd.notna(sma200_series.iloc[-1]) else None
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
    Fetch the 2 nearest genuinely-deferred contract prices and calculate
    spreads vs the front month.

    Returns list of dicts with:
        ticker, contract_name, month_name, commodity, price, spread
    """
    contracts = _select_deferred_contracts(commodity, n=2)
    results = []

    for cfg in contracts:
        tickers = cfg["tickers"]
        contract_name = cfg["contract_name"]
        month_name = cfg["month_name"]

        price = None
        used_ticker = None
        for ticker in tickers:
            try:
                logger.info(f"Fetching deferred contract {contract_name} ({ticker})")
                hist = yf.Ticker(ticker).history(period="5d")
                if not hist.empty:
                    hist = hist.sort_index()
                    price = float(hist["Close"].iloc[-1])
                    used_ticker = ticker
                    break
            except Exception as e:
                logger.debug(f"Deferred {ticker} failed: {e}")

        if price is None:
            logger.warning(f"No data found for deferred contract {contract_name} (tried {tickers})")
            continue

        spread = price - front_month_price
        results.append({
            "ticker": used_ticker,
            "contract_name": contract_name,
            "month_name": month_name,
            "commodity": commodity,
            "price": price,
            "spread": spread,
        })

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
