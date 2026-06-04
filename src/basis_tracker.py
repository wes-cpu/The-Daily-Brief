"""
basis_tracker.py - CSV storage and basis trend calculation.

Stores daily elevator bids to data/bids_history.csv and provides
trend analysis over 1-week, 2-week, and 1-month windows.
"""

import logging
import os
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
        if sub.empty:
            logger.debug(f"No non-null basis data for elevator='{elevator}' commodity='{commodity}'")
            return result
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
