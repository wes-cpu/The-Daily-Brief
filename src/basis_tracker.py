"""
basis_tracker.py - CSV storage and basis trend calculation.

Stores daily elevator bids to data/bids_history.csv and provides
trend analysis over 1-week, 2-week, and 1-month windows.
"""

import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# Load .env for local runs (GitHub Actions uses secrets instead)
load_dotenv()

logger = logging.getLogger(__name__)

# CSV path relative to repo root
_REPO_ROOT = Path(__file__).parent.parent
CSV_PATH = _REPO_ROOT / "data" / "bids_history.csv"

CSV_COLUMNS = [
    "date",
    "elevator",
    "commodity",
    "delivery_period",
    "cash_price",
    "basis",
    "futures_symbol",
]


def _ensure_csv_exists() -> None:
    """Create the CSV file with headers if it does not exist."""
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        df = pd.DataFrame(columns=CSV_COLUMNS)
        df.to_csv(CSV_PATH, index=False)
        logger.info(f"Created new CSV at {CSV_PATH}")


def _load_history() -> pd.DataFrame:
    """Load the full bid history CSV into a DataFrame."""
    _ensure_csv_exists()
    try:
        df = pd.read_csv(CSV_PATH, parse_dates=["date"])
        # Ensure correct dtypes
        df["cash_price"] = pd.to_numeric(df["cash_price"], errors="coerce")
        df["basis"] = pd.to_numeric(df["basis"], errors="coerce")
        return df
    except Exception as e:
        logger.error(f"Failed to load CSV from {CSV_PATH}: {e}")
        return pd.DataFrame(columns=CSV_COLUMNS)


def save_today_bids(bids: list[dict]) -> None:
    """
    Append today's bid records to the history CSV.

    bids: list of dicts from elevator_bids.py scrapers.
    Each dict must have keys: elevator, commodity, delivery_period,
    cash_price, basis, futures_reference (mapped to futures_symbol).
    """
    if not bids:
        logger.warning("save_today_bids called with empty bids list")
        return

    today_str = date.today().isoformat()
    rows = []
    for bid in bids:
        if bid.get("cash_price") is None:
            continue  # Skip bids with no price
        rows.append({
            "date": today_str,
            "elevator": bid.get("elevator", ""),
            "commodity": bid.get("commodity", ""),
            "delivery_period": bid.get("delivery_period", ""),
            "cash_price": bid.get("cash_price"),
            "basis": bid.get("basis"),
            "futures_symbol": bid.get("futures_reference", ""),
        })

    if not rows:
        logger.warning("No valid bids (with cash_price) to save")
        return

    new_df = pd.DataFrame(rows, columns=CSV_COLUMNS)

    _ensure_csv_exists()
    try:
        existing_df = _load_history()
        # Remove today's existing records for these elevators (avoid duplicates on re-run)
        if not existing_df.empty:
            today_dt = pd.Timestamp(today_str)
            elevators_today = new_df["elevator"].unique().tolist()
            mask_remove = (
                (existing_df["date"].dt.normalize() == today_dt) &
                (existing_df["elevator"].isin(elevators_today))
            )
            existing_df = existing_df[~mask_remove]

        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined.to_csv(CSV_PATH, index=False)
        logger.info(f"Saved {len(rows)} bid records for {today_str} to {CSV_PATH}")
    except Exception as e:
        logger.error(f"Failed to save bids to CSV: {e}", exc_info=True)
        raise


def _trend_direction(current: float, past: Optional[float]) -> str:
    """Return trend label comparing current basis to a past basis value."""
    if past is None:
        return "N/A"
    diff = current - past
    if abs(diff) < 0.5:  # less than half a cent — treat as unchanged
        return "unchanged"
    return "strengthening" if diff > 0 else "weakening"


def get_basis_trend(elevator: str, commodity: str) -> dict:
    """
    Return basis trend data for a specific elevator + commodity combination.

    Looks for the most recent "front-month" or first-available delivery period.

    Returns:
        {
            "current_basis": float or None,
            "basis_1week_ago": float or None,
            "basis_2weeks_ago": float or None,
            "basis_1month_ago": float or None,
            "trend_1week": "strengthening" / "weakening" / "unchanged" / "N/A",
            "trend_2week": ...,
            "trend_1month": ...,
        }
    """
    result = {
        "current_basis": None,
        "basis_1week_ago": None,
        "basis_2weeks_ago": None,
        "basis_1month_ago": None,
        "trend_1week": "N/A",
        "trend_2week": "N/A",
        "trend_1month": "N/A",
    }

    try:
        df = _load_history()
        if df.empty:
            return result

        # Filter for elevator + commodity
        mask = (
            (df["elevator"].str.lower() == elevator.lower()) &
            (df["commodity"].str.lower().str.contains(commodity.lower(), na=False))
        )
        sub = df[mask].copy()

        if sub.empty:
            logger.debug(f"No history for elevator='{elevator}' commodity='{commodity}'")
            return result

        sub = sub.dropna(subset=["basis"])
        sub = sub.sort_values("date")

        # Get the most common / earliest delivery period (front month proxy)
        # Use whichever delivery period appears most in recent data
        recent_cutoff = pd.Timestamp(date.today()) - timedelta(days=7)
        recent = sub[sub["date"] >= recent_cutoff]
        if recent.empty:
            recent = sub

        # Pick the delivery period with most appearances in recent data
        if recent["delivery_period"].dropna().empty:
            delivery_periods = sub["delivery_period"].value_counts()
        else:
            delivery_periods = recent["delivery_period"].value_counts()

        if delivery_periods.empty:
            target_delivery = None
        else:
            target_delivery = delivery_periods.index[0]

        # Filter to target delivery period (or use all if target is empty)
        if target_delivery:
            sub_del = sub[sub["delivery_period"] == target_delivery]
        else:
            sub_del = sub

        if sub_del.empty:
            sub_del = sub

        # Reference dates
        today = date.today()
        date_1week = today - timedelta(days=7)
        date_2week = today - timedelta(days=14)
        date_1month = today - timedelta(days=30)

        def _find_nearest_basis(target_date: date) -> Optional[float]:
            """Find the basis closest to target_date (within ±3 days)."""
            target_ts = pd.Timestamp(target_date)
            window = sub_del[
                (sub_del["date"] >= target_ts - timedelta(days=3)) &
                (sub_del["date"] <= target_ts + timedelta(days=3))
            ]
            if window.empty:
                return None
            # Return the record closest to target date
            window = window.copy()
            window["date_diff"] = (window["date"] - target_ts).abs()
            closest = window.nsmallest(1, "date_diff")
            val = closest["basis"].iloc[0]
            return float(val) if pd.notna(val) else None

        # Current basis: most recent record
        latest = sub_del.sort_values("date").iloc[-1]
        current_basis = float(latest["basis"]) if pd.notna(latest["basis"]) else None
        result["current_basis"] = current_basis

        if current_basis is not None:
            b1w = _find_nearest_basis(date_1week)
            b2w = _find_nearest_basis(date_2week)
            b1m = _find_nearest_basis(date_1month)

            result["basis_1week_ago"] = b1w
            result["basis_2weeks_ago"] = b2w
            result["basis_1month_ago"] = b1m
            result["trend_1week"] = _trend_direction(current_basis, b1w)
            result["trend_2week"] = _trend_direction(current_basis, b2w)
            result["trend_1month"] = _trend_direction(current_basis, b1m)

    except Exception as e:
        logger.error(f"get_basis_trend error for {elevator}/{commodity}: {e}", exc_info=True)

    return result


def get_all_basis_trends(all_bids: dict[str, list[dict]]) -> dict[str, dict]:
    """
    Compute basis trends for all elevators and commodities found in today's bids.

    Returns:
        { "Elevator Name|Corn": {trend dict}, ... }
    """
    trends = {}
    seen = set()
    for elevator_name, bids in all_bids.items():
        for bid in bids:
            commodity = bid.get("commodity", "")
            key = f"{elevator_name}|{commodity}"
            if key not in seen:
                seen.add(key)
                trends[key] = get_basis_trend(elevator_name, commodity)
    return trends


# ---------------------------------------------------------------------------
# Delivery period parsing + elevator deferred spread tracking
# ---------------------------------------------------------------------------

_FUTURES_MONTH_CODE: dict[str, int] = {
    "F": 1, "G": 2, "H": 3, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

_MONTH_NAMES_TO_NUM: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def _parse_delivery_period(period: str) -> Optional[tuple[int, int]]:
    """
    Parse a delivery period string into a (year, month) tuple for sorting.

    Handles: "JUL25", "Jul 2025", "July 2025", "N25", "ZCN25", "2025-07-15"
    Returns None if the period cannot be parsed.
    """
    if not period:
        return None
    p = period.strip()

    # ISO date "2025-07-15" or "2025-07"
    m = re.match(r"^(\d{4})-(\d{2})", p)
    if m:
        return int(m.group(1)), int(m.group(2))

    # "JUL25", "Jul 2025", "July 2025", "SEP2025"
    m = re.match(r"^([A-Za-z]{3,9})\s*(\d{2,4})$", p)
    if m:
        mon_str = m.group(1).lower()
        yr_raw = m.group(2)
        yr = 2000 + int(yr_raw) if len(yr_raw) == 2 else int(yr_raw)
        mon = _MONTH_NAMES_TO_NUM.get(mon_str)
        if mon:
            return yr, mon

    # Single-letter futures month code "N25"
    m = re.match(r"^([A-Za-z])(\d{2})$", p)
    if m:
        code = m.group(1).upper()
        yr = 2000 + int(m.group(2))
        mon = _FUTURES_MONTH_CODE.get(code)
        if mon:
            return yr, mon

    # Embedded futures code in ticker like "ZCN25"
    m = re.search(r"([FGHKMNQUVXZ])(\d{2})\b", p.upper())
    if m:
        code = m.group(1)
        yr = 2000 + int(m.group(2))
        mon = _FUTURES_MONTH_CODE.get(code)
        if mon:
            return yr, mon

    return None


def get_elevator_spread_data(
    today_bids_by_elevator: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """
    For each elevator/commodity, identify the front-month bid and compute
    spreads to every deferred delivery period.  Compare today's spreads to
    the most recent prior-day spreads stored in bids_history.csv.

    Returns dict keyed by elevator name; each value is a list of spread records:
        {
            "commodity": str,
            "front_period": str,
            "front_cash": float,
            "front_basis": float | None,
            "deferred_period": str,
            "deferred_cash": float,
            "deferred_basis": float | None,
            "cash_spread_today": float,        # deferred_cash - front_cash
            "basis_spread_today": float | None,
            "cash_spread_prev": float | None,  # same spread from prior day
            "basis_spread_prev": float | None,
            "cash_spread_change": float | None,
            "basis_spread_change": float | None,
        }
    """
    df = _load_history()
    today_ts = pd.Timestamp(date.today())
    result: dict[str, list[dict]] = {}

    for elevator_name, bids in today_bids_by_elevator.items():
        if not bids:
            continue

        # Group today's bids by commodity
        by_commodity: dict[str, list[dict]] = {}
        for bid in bids:
            commodity = bid.get("commodity", "")
            if commodity:
                by_commodity.setdefault(commodity, []).append(bid)

        elevator_spreads: list[dict] = []

        for commodity, cbids in by_commodity.items():
            # Sort by parsed delivery period (front month first)
            parsed: list[tuple[tuple[int, int], dict]] = []
            for bid in cbids:
                period = bid.get("delivery_period", "")
                key = _parse_delivery_period(period) or (9999, 99)
                parsed.append((key, bid))
            parsed.sort(key=lambda x: x[0])

            if len(parsed) < 2:
                continue

            _, front_bid = parsed[0]
            front_period = front_bid.get("delivery_period", "")
            front_cash = front_bid.get("cash_price")
            front_basis = front_bid.get("basis")

            if front_cash is None:
                continue

            # Load prior-day bids for spread comparison (up to 5 days back for weekends)
            hist_sub: Optional[pd.DataFrame] = None
            if not df.empty:
                hist_mask = (
                    (df["elevator"].str.lower() == elevator_name.lower()) &
                    (df["commodity"].str.lower().str.contains(commodity.lower(), na=False)) &
                    (df["date"] < today_ts) &
                    (df["date"] >= today_ts - pd.Timedelta(days=5))
                )
                sub = df[hist_mask].copy()
                if not sub.empty:
                    last_date = sub["date"].max()
                    hist_sub = sub[sub["date"] == last_date]

            for deferred_key, deferred_bid in parsed[1:]:
                deferred_period = deferred_bid.get("delivery_period", "")
                deferred_cash = deferred_bid.get("cash_price")
                deferred_basis = deferred_bid.get("basis")

                if deferred_cash is None:
                    continue

                cash_spread_today = deferred_cash - front_cash
                basis_spread_today = (
                    (deferred_basis - front_basis)
                    if deferred_basis is not None and front_basis is not None
                    else None
                )

                cash_spread_prev: Optional[float] = None
                basis_spread_prev: Optional[float] = None
                cash_spread_change: Optional[float] = None
                basis_spread_change: Optional[float] = None

                if hist_sub is not None and not hist_sub.empty:
                    hist_parsed: list[tuple[tuple[int, int], pd.Series]] = []
                    for _, row in hist_sub.iterrows():
                        pkey = _parse_delivery_period(str(row["delivery_period"])) or (9999, 99)
                        hist_parsed.append((pkey, row))
                    hist_parsed.sort(key=lambda x: x[0])

                    if len(hist_parsed) >= 2:
                        _, h_front = hist_parsed[0]
                        h_front_cash = float(h_front["cash_price"]) if pd.notna(h_front["cash_price"]) else None
                        h_front_basis = float(h_front["basis"]) if pd.notna(h_front["basis"]) else None

                        for h_def_key, h_def in hist_parsed[1:]:
                            if h_def_key == deferred_key:
                                h_def_cash = float(h_def["cash_price"]) if pd.notna(h_def["cash_price"]) else None
                                h_def_basis = float(h_def["basis"]) if pd.notna(h_def["basis"]) else None
                                if h_front_cash is not None and h_def_cash is not None:
                                    cash_spread_prev = h_def_cash - h_front_cash
                                    cash_spread_change = cash_spread_today - cash_spread_prev
                                if h_front_basis is not None and h_def_basis is not None:
                                    basis_spread_prev = h_def_basis - h_front_basis
                                    if basis_spread_today is not None:
                                        basis_spread_change = basis_spread_today - basis_spread_prev
                                break

                elevator_spreads.append({
                    "commodity": commodity,
                    "front_period": front_period,
                    "front_cash": front_cash,
                    "front_basis": front_basis,
                    "deferred_period": deferred_period,
                    "deferred_cash": deferred_cash,
                    "deferred_basis": deferred_basis,
                    "cash_spread_today": cash_spread_today,
                    "basis_spread_today": basis_spread_today,
                    "cash_spread_prev": cash_spread_prev,
                    "basis_spread_prev": basis_spread_prev,
                    "cash_spread_change": cash_spread_change,
                    "basis_spread_change": basis_spread_change,
                })

        if elevator_spreads:
            result[elevator_name] = elevator_spreads

    return result
