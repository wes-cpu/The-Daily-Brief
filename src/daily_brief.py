"""
daily_brief.py - Main orchestrator for the Daily Grain Brief pipeline.

Entry point: python -m src.daily_brief

Steps:
  1. Fetch CBOT futures prices + technical indicators
  2. Scrape cash bids from 10 elevator websites
  3. Save today's bids to CSV for basis trend tracking
  4. Compute basis trends (1-week, 2-week, 1-month)
  5. Build HTML email
  6. Send email via Gmail SMTP
"""

import asyncio
import logging
import sys
from datetime import date

from dotenv import load_dotenv

# Load .env first, before any other imports that might need env vars
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("daily_brief")


def main() -> None:
    """Main orchestration function."""
    report_date = date.today()
    logger.info(f"=== Daily Grain Brief starting for {report_date.isoformat()} ===")

    # ------------------------------------------------------------------
    # Step 1: Fetch futures data
    # ------------------------------------------------------------------
    logger.info("--- Step 1: Fetching CBOT futures prices ---")
    futures_data = {"front_month": {}, "deferred": {}}
    try:
        from src.futures import fetch_all_futures
        futures_data = fetch_all_futures()
        front_count = sum(1 for v in futures_data["front_month"].values() if v is not None)
        deferred_count = sum(len(v) for v in futures_data["deferred"].values())
        logger.info(f"Futures: {front_count}/3 front-month contracts, {deferred_count} deferred contracts loaded")
    except Exception as e:
        logger.error(f"Futures fetch failed: {e}", exc_info=True)
        logger.warning("Continuing with empty futures data")

    # ------------------------------------------------------------------
    # Step 2: Scrape elevator bids
    # ------------------------------------------------------------------
    logger.info("--- Step 2: Scraping elevator cash bids ---")
    elevator_bids: dict[str, list[dict]] = {}
    scoular_fresh_cookies: list[dict] = []
    try:
        from src.elevator_bids import fetch_all_elevator_bids
        elevator_bids, scoular_fresh_cookies = asyncio.run(fetch_all_elevator_bids())
        success_count = sum(1 for v in elevator_bids.values() if v)
        fail_count = sum(1 for v in elevator_bids.values() if not v)
        total_bids = sum(len(v) for v in elevator_bids.values())
        logger.info(
            f"Elevator bids: {success_count} elevators loaded, "
            f"{fail_count} failed, {total_bids} total bid records"
        )
        for name, bids in elevator_bids.items():
            if not bids:
                logger.warning(f"  FAILED: {name}")
            else:
                logger.info(f"  OK: {name} ({len(bids)} bids)")
    except Exception as e:
        logger.error(f"Elevator bid scraping failed: {e}", exc_info=True)
        logger.warning("Continuing with empty elevator data")

    # ------------------------------------------------------------------
    # Step 2b: Auto-rotate Scoular session cookies (keeps login alive forever)
    # ------------------------------------------------------------------
    if scoular_fresh_cookies:
        logger.info("--- Step 2b: Rotating SCOULAR_COOKIES secret ---")
        try:
            from src.secret_updater import refresh_scoular_cookies
            refresh_scoular_cookies(scoular_fresh_cookies)
        except Exception as e:
            logger.error(f"Cookie rotation failed (non-fatal): {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Step 3: Save today's bids to CSV
    # ------------------------------------------------------------------
    logger.info("--- Step 3: Saving bids to CSV history ---")
    all_bids_flat: list[dict] = []
    for bids in elevator_bids.values():
        all_bids_flat.extend(bids)

    if all_bids_flat:
        try:
            from src.basis_tracker import save_today_bids
            save_today_bids(all_bids_flat)
            logger.info(f"Saved {len(all_bids_flat)} bid records to CSV")
        except Exception as e:
            logger.error(f"Failed to save bids to CSV: {e}", exc_info=True)
            logger.warning("Continuing without CSV save — basis trends may be incomplete")
    else:
        logger.warning("No bids to save to CSV")

    # ------------------------------------------------------------------
    # Step 4: Compute basis trends and elevator spreads
    # ------------------------------------------------------------------
    logger.info("--- Step 4: Computing basis trends and elevator spreads ---")
    basis_trends: dict[str, dict] = {}
    elevator_spreads: dict[str, list] = {}
    try:
        from src.basis_tracker import get_all_basis_trends, get_elevator_spread_changes
        basis_trends = get_all_basis_trends(elevator_bids)
        logger.info(f"Computed basis trends for {len(basis_trends)} elevator/commodity combinations")
        elevator_spreads = get_elevator_spread_changes(elevator_bids)
        spread_count = sum(len(v) for v in elevator_spreads.values())
        logger.info(f"Computed {spread_count} elevator front/deferred spread records")
    except Exception as e:
        logger.error(f"Basis trend/spread computation failed: {e}", exc_info=True)
        logger.warning("Continuing without basis trends/spreads")

    # ------------------------------------------------------------------
    # Step 5: Build HTML email
    # ------------------------------------------------------------------
    logger.info("--- Step 5: Building HTML email ---")
    html_body = ""
    try:
        from src.email_builder import build_email
        html_body = build_email(
            futures_data=futures_data,
            elevator_bids=elevator_bids,
            basis_trends=basis_trends,
            elevator_spreads=elevator_spreads,
            report_date=report_date,
        )
        logger.info(f"HTML email built ({len(html_body):,} bytes)")
    except Exception as e:
        logger.error(f"Email build failed: {e}", exc_info=True)
        # Build a minimal fallback email so we can still send something
        html_body = _build_fallback_email(report_date, str(e))
        logger.warning("Using minimal fallback email")

    # ------------------------------------------------------------------
    # Step 6: Send email
    # ------------------------------------------------------------------
    logger.info("--- Step 6: Sending email via Gmail SMTP ---")
    try:
        from src.mailer import send_email
        send_email(html_body, report_date=report_date)
        logger.info("Email sent successfully")
    except Exception as e:
        logger.error(f"Email send FAILED: {e}", exc_info=True)
        logger.error("=== Daily Grain Brief FAILED at email send step ===")
        sys.exit(1)

    logger.info("=== Daily Grain Brief completed successfully ===")


def _build_fallback_email(report_date: date, error_message: str) -> str:
    """Build a minimal fallback HTML email when the main builder fails."""
    date_str = report_date.strftime("%B %d, %Y")
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;padding:20px;">
  <h1 style="color:#2d5a1b;">Daily Grain Brief — {date_str}</h1>
  <p style="color:#dc2626;font-weight:bold;">
    ⚠ The email builder encountered an error. Please check the GitHub Actions logs.
  </p>
  <pre style="background:#f3f4f6;padding:12px;border-radius:4px;font-size:12px;">
{error_message}
  </pre>
  <p>Data collection may have partially succeeded. Review logs for details.</p>
</body>
</html>"""


if __name__ == "__main__":
    main()
