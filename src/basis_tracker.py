"""
basis_tracker.py - CSV storage and basis / futures-spread trend calculation.

Stores daily elevator bids to data/bids_history.csv and futures prices to
data/futures_history.csv.  Provides trend analysis over 1-week, 2-week, and
1-month windows for both basis and deferred futures spreads.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent
CSV_PATH = _REPO_ROOT / "data" / "bids_history.csv"
FUTURES_CSV_PATH = _REPO_ROOT / "data" / "futures_history.csv"

CSV_COLUMNS = [
    "date",
    "elevator",
    "commodity",
    "delivery_period",
    "cash_price",
    "basis",
    "futures_symbol",
]

FUTURES_CSV_COLUMNS = [
    "date",
    "commodity",
    "contract",      # e.g. "front_month", "ZCU2026", "ZCZ2026"
    "price",
    "spread_vs_front",  # None for front_month row
]


def _ensure_csv_exists() -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        pd.DataFrame(columns=CSV_COLUMNS).to_csv(CSV_PATH, index=False)
        logger.info(f"Created new bids CSV at {CSV_PATH}")


def _ensure_futures_csv_exists() -> None:
    FUTURES_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not FUTURES_CSV_PATH.exists():
        pd.DataFrame(columns=FUTURES_CSV_COLUMNS).to_csv(FUTURES_CSV_PATH, index=False)
        logger.info(f"Created new futures CSV at {FUTURES_CSV_PATH}")


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
            (df["commodity"].str.lower().str.contains(commodity.lower(), na=False, regex=False))
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


def save_today_futures(futures_data: dict) -> None:
    """
    Append today's futures prices and deferred spreads to futures_history.csv.

    futures_data: return value of fetch_all_futures()
    """
    today_str = date.today().isoformat()
    rows = []

    front_month = futures_data.get("front_month", {})
    deferred = futures_data.get("deferred", {})

    for commodity in ("corn", "soybeans", "wheat"):
        fm = front_month.get(commodity)
        if fm is None:
            continue

        rows.append({
            "date": today_str,
            "commodity": commodity,
            "contract": "front_month",
            "price": fm.get("price"),
            "spread_vs_front": None,
        })

        for dc in deferred.get(commodity, []):
            rows.append({
                "date": today_str,
                "commodity": commodity,
                "contract": dc.get("contract_name", ""),
                "price": dc.get("price"),
                "spread_vs_front": dc.get("spread"),
            })

    if not rows:
        logger.warning("save_today_futures: no futures data to save")
        return

    new_df = pd.DataFrame(rows, columns=FUTURES_CSV_COLUMNS)
    _ensure_futures_csv_exists()

    try:
        existing_df = _load_futures_history()
        if not existing_df.empty:
            today_ts = pd.Timestamp(today_str)
            mask = existing_df["date"].dt.normalize() == today_ts
            existing_df = existing_df[~mask]
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined.to_csv(FUTURES_CSV_PATH, index=False)
        logger.info(f"Saved {len(rows)} futures rows for {today_str}")
    except Exception as e:
        logger.error(f"Failed to save futures to CSV: {e}", exc_info=True)


def _load_futures_history() -> pd.DataFrame:
    _ensure_futures_csv_exists()
    try:
        df = pd.read_csv(FUTURES_CSV_PATH, parse_dates=["date"])
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["spread_vs_front"] = pd.to_numeric(df["spread_vs_front"], errors="coerce")
        return df
    except Exception as e:
        logger.error(f"Failed to load futures CSV: {e}")
        return pd.DataFrame(columns=FUTURES_CSV_COLUMNS)


def get_deferred_spread_trends(futures_data: dict) -> dict[str, dict]:
    """
    For each deferred contract compare today's spread to 1-day, 1-week, and
    1-month prior, returning a dict keyed by "commodity|contract_name".

    Return shape per key:
        {
            "contract_name": str,
            "month_name": str,
            "current_spread": float | None,
            "spread_1day_ago": float | None,
            "spread_1week_ago": float | None,
            "spread_1month_ago": float | None,
            "change_1day": float | None,   # positive = carry widened (contango deepened)
            "change_1week": float | None,
            "change_1month": float | None,
        }
    """
    df = _load_futures_history()
    today = date.today()
    result: dict[str, dict] = {}

    deferred = futures_data.get("deferred", {})

    for commodity, contracts in deferred.items():
        for dc in contracts:
            contract_name = dc.get("contract_name", "")
            month_name = dc.get("month_name", "")
            current_spread = dc.get("spread")

            key = f"{commodity}|{contract_name}"
            entry: dict = {
                "contract_name": contract_name,
                "month_name": month_name,
                "current_spread": current_spread,
                "spread_1day_ago": None,
                "spread_1week_ago": None,
                "spread_1month_ago": None,
                "change_1day": None,
                "change_1week": None,
                "change_1month": None,
            }

            if df.empty or current_spread is None:
                result[key] = entry
                continue

            mask = (
                (df["commodity"] == commodity)
                & (df["contract"] == contract_name)
                & (~df["spread_vs_front"].isna())
            )
            sub = df[mask].sort_values("date")
            if sub.empty:
                result[key] = entry
                continue

            def _nearest_spread(target: date) -> Optional[float]:
                ts = pd.Timestamp(target)
                window = sub[
                    (sub["date"] >= ts - timedelta(days=3))
                    & (sub["date"] <= ts + timedelta(days=3))
                ].copy()
                if window.empty:
                    return None
                window["diff"] = (window["date"] - ts).abs()
                row = window.nsmallest(1, "diff")
                val = row["spread_vs_front"].iloc[0]
                return float(val) if pd.notna(val) else None

            s1d = _nearest_spread(today - timedelta(days=1))
            s1w = _nearest_spread(today - timedelta(days=7))
            s1m = _nearest_spread(today - timedelta(days=30))

            entry.update({
                "spread_1day_ago": s1d,
                "spread_1week_ago": s1w,
                "spread_1month_ago": s1m,
                "change_1day": round(current_spread - s1d, 4) if s1d is not None else None,
                "change_1week": round(current_spread - s1w, 4) if s1w is not None else None,
                "change_1month": round(current_spread - s1m, 4) if s1m is not None else None,
            })
            result[key] = entry

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
