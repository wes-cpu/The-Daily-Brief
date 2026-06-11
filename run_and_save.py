"""
Standalone runner: collects live data and saves HTML email to /tmp/brief.html
Does NOT send via SMTP — output is consumed by the caller.
"""

import asyncio
import logging
import sys
from datetime import date

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
logger = logging.getLogger("runner")

def main():
    report_date = date.today()
    logger.info(f"=== Daily Grain Brief — data collection for {report_date.isoformat()} ===")

    # Step 1: Futures
    futures_data = {"front_month": {}, "deferred": {}}
    try:
        from src.futures import fetch_all_futures
        futures_data = fetch_all_futures()
        logger.info("Futures data loaded")
    except Exception as e:
        logger.error(f"Futures failed: {e}", exc_info=True)

    # Step 2: Elevator bids
    elevator_bids = {}
    scoular_fresh_cookies = []
    try:
        from src.elevator_bids import fetch_all_elevator_bids
        elevator_bids, scoular_fresh_cookies = asyncio.run(fetch_all_elevator_bids())
        total = sum(len(v) for v in elevator_bids.values())
        logger.info(f"Elevator bids: {total} total records")
    except Exception as e:
        logger.error(f"Elevator scraping failed: {e}", exc_info=True)

    # Step 3: Save CSV
    all_bids_flat = [b for bids in elevator_bids.values() for b in bids]
    if all_bids_flat:
        try:
            from src.basis_tracker import save_today_bids
            save_today_bids(all_bids_flat)
        except Exception as e:
            logger.error(f"CSV save failed: {e}")

    # Step 4: Basis trends
    basis_trends = {}
    try:
        from src.basis_tracker import get_all_basis_trends
        basis_trends = get_all_basis_trends(elevator_bids)
    except Exception as e:
        logger.error(f"Basis trends failed: {e}")

    # Step 5: Build HTML
    try:
        from src.email_builder import build_email
        html = build_email(
            futures_data=futures_data,
            elevator_bids=elevator_bids,
            basis_trends=basis_trends,
            report_date=report_date,
        )
        with open("/tmp/brief.html", "w") as f:
            f.write(html)
        logger.info(f"HTML saved to /tmp/brief.html ({len(html):,} bytes)")
        print("SUCCESS")
    except Exception as e:
        logger.error(f"Email build failed: {e}", exc_info=True)
        print("FAILED")

if __name__ == "__main__":
    main()
