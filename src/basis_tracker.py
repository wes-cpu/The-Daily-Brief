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


# ---------------------------------------------------------------------------
# Elevator deferred spread tracking
# ---------------------------------------------------------------------------

_MONTH_ABBR_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def _parse_delivery_sort_key(period: str) -> tuple[int, int]:
    """Parse a delivery period string to (year, month) for chronological sorting."""
    s = str(period).strip()
    if not s:
        return (9999, 99)

    # ISO format: "2025-07-01" or "2025-07-01T00:00:00Z"
    m = re.match(r'^(\d{4})-(\d{1,2})', s)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    # MM/DD/YYYY or MM/YYYY
    m = re.match(r'^(\d{1,2})/(?:\d{1,2}/)?(\d{4})', s)
    if m:
        return (int(m.group(2)), int(m.group(1)))

    # "Jul 2025", "July 2025", "Sep/Oct 2025" (use first month found)
    s_lower = s.lower()
    ym = re.search(r'\b(20\d{2})\b', s)
    year = int(ym.group(1)) if ym else 9999
    for abbr, mnum in _MONTH_ABBR_MAP.items():
        if abbr in s_lower:
            return (year, mnum)

    return (9999, 99)


def _sort_delivery_periods(periods: list[str]) -> list[str]:
    """Sort delivery period strings chronologically (earliest first)."""
    return sorted(periods, key=_parse_delivery_sort_key)


def _compute_elevator_spread_on_date(
    df: pd.DataFrame,
    elevator: str,
    commodity: str,
    deferred_period: str,
    target_date: date,
) -> Optional[float]:
    """
    Look up the spread (deferred_basis − front_basis) for a specific
    elevator+commodity and deferred_period on or near target_date.
    Returns None if insufficient data exists.
    """
    if df.empty:
        return None

    mask = (
        (df["elevator"].str.lower() == elevator.lower()) &
        (df["commodity"].str.lower().str.contains(commodity.lower(), na=False))
    )
    sub = df[mask].dropna(subset=["basis"]).copy()
    if sub.empty:
        return None

    target_ts = pd.Timestamp(target_date)
    window = sub[
        (sub["date"] >= target_ts - timedelta(days=3)) &
        (sub["date"] <= target_ts + timedelta(days=3))
    ].copy()
    if window.empty:
        return None

    window["_dd"] = (window["date"] - target_ts).abs()
    closest_dt = window["_dd"].min()
    on_date = window[window["_dd"] == closest_dt]

    sorted_periods = _sort_delivery_periods(
        on_date["delivery_period"].dropna().unique().tolist()
    )
    if not sorted_periods:
        return None

    front_hist = sorted_periods[0]
    front_row = on_date[on_date["delivery_period"] == front_hist]
    if front_row.empty:
        return None
    front_basis = float(front_row["basis"].iloc[0])

    deferred_row = on_date[on_date["delivery_period"] == deferred_period]
    if deferred_row.empty:
        return None
    deferred_basis = float(deferred_row["basis"].iloc[0])

    return deferred_basis - front_basis


def _spread_direction(current: float, past: Optional[float]) -> str:
    """Return 'widening'/'narrowing'/'unchanged'/'N/A' for a spread change."""
    if past is None:
        return "N/A"
    diff = current - past
    if abs(diff) < 0.5:
        return "unchanged"
    return "widening" if diff > 0 else "narrowing"


def get_elevator_deferred_spread_trends(
    all_bids: dict[str, list[dict]],
) -> dict[str, dict]:
    """
    For each elevator+commodity with ≥2 delivery periods in today's bids,
    compute the basis spread (deferred basis − front month basis) for every
    deferred period and compare to 1-week, 2-week, and 1-month historical data.

    Returns:
        {
          "ElevatorName|Commodity": {
            "front_period": "Jul 2025",
            "spreads": [
              {
                "deferred_period": "Sep 2025",
                "current_spread": +10.0,    # ¢/bu, positive = inverse
                "spread_1week_ago": +8.0,
                "spread_2week_ago": None,
                "spread_1month_ago": None,
                "trend_1week": "widening",
                "trend_2week": "N/A",
                "trend_1month": "N/A",
              }, ...
            ]
          }, ...
        }
    """
    result: dict[str, dict] = {}
    today = date.today()
    date_1week = today - timedelta(days=7)
    date_2week = today - timedelta(days=14)
    date_1month = today - timedelta(days=30)

    try:
        df = _load_history()
    except Exception as exc:
        logger.error(f"get_elevator_deferred_spread_trends: load error: {exc}")
        df = pd.DataFrame(columns=CSV_COLUMNS)

    for elevator_name, bids in all_bids.items():
        if not bids:
            continue

        # Collect {commodity: {delivery_period: basis}} from today's bids
        by_commodity: dict[str, dict[str, float]] = {}
        for bid in bids:
            commodity = bid.get("commodity", "")
            dp = bid.get("delivery_period", "")
            basis = bid.get("basis")
            if commodity and dp and basis is not None:
                by_commodity.setdefault(commodity, {})[dp] = float(basis)

        for commodity, period_basis in by_commodity.items():
            if len(period_basis) < 2:
                continue  # Need at least front + one deferred

            sorted_periods = _sort_delivery_periods(list(period_basis.keys()))
            front_period = sorted_periods[0]
            front_basis_today = period_basis[front_period]

            spreads = []
            for dp in sorted_periods[1:]:
                spread_today = period_basis[dp] - front_basis_today
                s1w = _compute_elevator_spread_on_date(df, elevator_name, commodity, dp, date_1week)
                s2w = _compute_elevator_spread_on_date(df, elevator_name, commodity, dp, date_2week)
                s1m = _compute_elevator_spread_on_date(df, elevator_name, commodity, dp, date_1month)

                spreads.append({
                    "deferred_period": dp,
                    "current_spread": spread_today,
                    "spread_1week_ago": s1w,
                    "spread_2week_ago": s2w,
                    "spread_1month_ago": s1m,
                    "trend_1week": _spread_direction(spread_today, s1w),
                    "trend_2week": _spread_direction(spread_today, s2w),
                    "trend_1month": _spread_direction(spread_today, s1m),
                })

            if spreads:
                result[f"{elevator_name}|{commodity}"] = {
                    "front_period": front_period,
                    "spreads": spreads,
                }

    return result
