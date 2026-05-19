"""
get_scoular_cookies.py — run this ONE TIME on your local machine to capture
Scoular session cookies after a manual login (including MFA).

Usage:
    pip install playwright
    playwright install chromium
    python get_scoular_cookies.py

Then copy the printed JSON and save it as the SCOULAR_COOKIES GitHub secret.
Repeat when cookies expire (typically every 30-90 days).
"""

import asyncio
import json
from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print("Opening Scoular login page…")
        await page.goto("https://scoularview.com/cbloc-1941")

        print("\n" + "=" * 60)
        print("A browser window just opened.")
        print("Log in normally — enter your username, password,")
        print("and the security code sent to your email.")
        print("Wait until you can SEE YOUR GRAIN BIDS on screen.")
        print("=" * 60)
        input("\nOnce the bids are visible, press Enter here: ")

        cookies = await context.cookies()
        await browser.close()

    cookie_json = json.dumps(cookies)

    print("\n" + "=" * 60)
    print("SUCCESS — copy EVERYTHING between the lines below")
    print("and save it as the SCOULAR_COOKIES GitHub secret:")
    print("=" * 60 + "\n")
    print(cookie_json)
    print("\n" + "=" * 60)
    print(f"({len(cookies)} cookies captured)")


if __name__ == "__main__":
    asyncio.run(main())
