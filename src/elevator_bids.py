"""
elevator_bids.py - Playwright-based scrapers for 10 grain elevator websites.

Each scraper function is async and returns a list of bid dicts:
    {
        "elevator": str,
        "commodity": str,
        "delivery_period": str,
        "cash_price": float,
        "basis": float,
        "futures_reference": str,
        "change": float or None,
    }
"""

import asyncio
import email as email_lib
import imaplib
import json
import logging
import os
import re
import time
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
DEFAULT_TIMEOUT = 30_000  # 30 seconds in ms


# ---------------------------------------------------------------------------
# IMAP helper — reads Scoular MFA code from wes@seifert.farm (Google Workspace)
# ---------------------------------------------------------------------------

def _fetch_scoular_mfa_code(
    imap_user: str,
    imap_pass: str,
    imap_host: str = "imap.gmail.com",
    wait_seconds: int = 60,
    poll_interval: int = 5,
) -> Optional[str]:
    """
    Poll the inbox at imap_user (Google Workspace / Gmail) for a Scoular
    security-code email, retrying every poll_interval seconds for up to
    wait_seconds total.  Returns the extracted numeric code, or None on failure.
    """
    deadline = time.monotonic() + wait_seconds
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            with imaplib.IMAP4_SSL(imap_host) as mail:
                mail.login(imap_user, imap_pass)
                mail.select("INBOX")

                # Search for recent Scoular emails (last 10 minutes is plenty)
                # SINCE is date-only in IMAP; combine with a body/subject search.
                status, msg_ids = mail.search(
                    None,
                    '(FROM "scoular" SUBJECT "security" UNSEEN)',
                )
                if status != "OK" or not msg_ids[0]:
                    # Broaden: any unseen Scoular email
                    status, msg_ids = mail.search(None, '(FROM "scoular" UNSEEN)')

                if status == "OK" and msg_ids[0]:
                    ids = msg_ids[0].split()
                    # Check the most recent matching message
                    raw = mail.fetch(ids[-1], "(RFC822)")[1][0][1]
                    msg = email_lib.message_from_bytes(raw)

                    # Walk all parts looking for the code
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(errors="replace")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="replace")

                    # Scoular codes are typically 6-digit numbers
                    match = re.search(r"\b(\d{6})\b", body)
                    if match:
                        code = match.group(1)
                        logger.info(f"Scoular MFA: found code {code} in email (attempt {attempt})")
                        # Mark the message as seen so we don't re-use it
                        mail.store(ids[-1], "+FLAGS", "\\Seen")
                        return code
                    else:
                        logger.debug(f"Scoular MFA: email found but no 6-digit code in body (attempt {attempt})")
                else:
                    logger.debug(f"Scoular MFA: no matching email yet (attempt {attempt})")

        except imaplib.IMAP4.error as exc:
            logger.error(f"Scoular MFA IMAP error: {exc}")
            return None  # auth failure — no point retrying

        remaining = deadline - time.monotonic()
        if remaining > 0:
            sleep_for = min(poll_interval, remaining)
            logger.info(f"Scoular MFA: waiting {sleep_for:.0f}s for code email… ({remaining:.0f}s remaining)")
            time.sleep(sleep_for)

    logger.error(f"Scoular MFA: code not received within {wait_seconds}s")
    return None


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _parse_price(raw: str) -> Optional[float]:
    """Parse a price string like '4.50', '450', '$4.50' into a float."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", raw.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_basis(raw: str) -> Optional[float]:
    """Parse a basis string like '-25', '+5', '25' into a float (cents)."""
    if not raw:
        return None
    cleaned = raw.strip().replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


async def _new_page(context: BrowserContext, timeout: int = DEFAULT_TIMEOUT) -> Page:
    page = await context.new_page()
    page.set_default_timeout(timeout)
    return page


# ---------------------------------------------------------------------------
# Barchart WebSol helper: network-intercept getCashBids API
# ---------------------------------------------------------------------------

async def _fetch_barchart_bids(
    page: Page,
    url: str,
    elevator_name: str,
) -> list[dict]:
    """
    For sites powered by Barchart WebSol: intercept the getCashBids API call.
    Falls back to parsing rendered HTML table on failure.
    """
    captured_bids: list[dict] = []
    api_captured = False

    # Barchart WebSol cash-bid APIs use several URL patterns depending on the
    # widget version.  Widen the net to catch all of them.
    BARCHART_PATTERNS = (
        "getCashBids",
        "cash_bids",
        "cashbids",
        "websol.barchart.com",
        "ondemand.barchart.com",
        "www-api.barchart.com",
        "/v2/cashbids",
        "/v2/cash-bids",
    )

    async def handle_response(response):
        nonlocal api_captured
        try:
            rurl = response.url
            if not any(pat.lower() in rurl.lower() for pat in BARCHART_PATTERNS):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await response.json()
            api_captured = True
            # Barchart API structure varies; try common shapes
            bids_data = (
                body.get("data", {}).get("cashBids", [])
                or body.get("cashBids", [])
                or body.get("results", [])
                or (body.get("data", []) if isinstance(body.get("data"), list) else [])
            )
            if isinstance(bids_data, list):
                for item in bids_data:
                    commodity = (
                        item.get("commodity", item.get("name", "Unknown"))
                        .strip()
                    )
                    captured_bids.append({
                        "elevator": elevator_name,
                        "commodity": commodity,
                        "delivery_period": item.get("expirationDate", item.get("deliveryPeriod", "")),
                        "cash_price": _parse_price(str(item.get("cashPrice", item.get("price", "")))),
                        "basis": _parse_basis(str(item.get("basis", ""))),
                        "futures_reference": item.get("futuresCode", item.get("contractCode", "")),
                        "change": _parse_price(str(item.get("netChange", item.get("change", "")))),
                    })
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        await page.goto(url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        logger.warning(f"{elevator_name}: navigation error: {e}")

    # Barchart widgets fire their API call after networkidle — give extra time.
    # Also try waiting for bid-table selectors to confirm the widget has rendered.
    WIDGET_SELECTORS = [
        "[class*='cash-bid']",
        "[class*='cashbid']",
        "[class*='CashBid']",
        ".bc-cash-bids",
        "[data-module='CashBids']",
        "table tbody tr td",
    ]
    for sel in WIDGET_SELECTORS:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            break
        except Exception:
            continue

    await asyncio.sleep(4)

    if api_captured and captured_bids:
        logger.info(f"{elevator_name}: captured {len(captured_bids)} bids via API intercept")
        return captured_bids

    # Fallback: parse HTML table
    logger.info(f"{elevator_name}: API intercept missed; falling back to HTML parse")
    return await _parse_barchart_html(page, elevator_name)


async def _parse_barchart_html(page: Page, elevator_name: str) -> list[dict]:
    """Parse rendered Barchart cash-bid table from page HTML."""
    bids = []
    try:
        # Try multiple possible selectors
        selectors = [
            "table.cash-bids",
            "table.bids-table",
            ".cashbid-table table",
            "table[class*='cashbid']",
            "table[class*='cash-bid']",
            "table[class*='bids']",
            "div.cashbid",
            "table",
        ]
        table = None
        for sel in selectors:
            try:
                table = await page.query_selector(sel)
                if table:
                    break
            except Exception:
                continue

        if not table:
            logger.warning(f"{elevator_name}: no table found in HTML fallback")
            return bids

        rows = await table.query_selector_all("tr")
        header_row = rows[0] if rows else None
        headers = []
        if header_row:
            ths = await header_row.query_selector_all("th, td")
            headers = [await th.inner_text() for th in ths]
            headers = [h.strip().lower() for h in headers]

        for row in rows[1:]:
            cells = await row.query_selector_all("td")
            if not cells:
                continue
            vals = [await c.inner_text() for c in cells]
            vals = [v.strip() for v in vals]

            # Map columns heuristically
            commodity = vals[0] if len(vals) > 0 else ""
            delivery = vals[1] if len(vals) > 1 else ""
            cash_raw = vals[2] if len(vals) > 2 else ""
            basis_raw = vals[3] if len(vals) > 3 else ""
            futures_ref = vals[4] if len(vals) > 4 else ""
            change_raw = vals[5] if len(vals) > 5 else ""

            # If we have header info, use it
            if headers:
                col_map = {h: i for i, h in enumerate(headers)}
                commodity = vals[col_map.get("commodity", col_map.get("grain", 0))] if col_map.get("commodity") is not None or col_map.get("grain") is not None else commodity
                delivery = vals[col_map.get("delivery", col_map.get("period", col_map.get("month", 1)))] if len(vals) > 1 else delivery

            cash_price = _parse_price(cash_raw)
            basis = _parse_basis(basis_raw)
            if commodity and cash_price is not None:
                bids.append({
                    "elevator": elevator_name,
                    "commodity": commodity,
                    "delivery_period": delivery,
                    "cash_price": cash_price,
                    "basis": basis,
                    "futures_reference": futures_ref,
                    "change": _parse_price(change_raw),
                })
    except Exception as e:
        logger.error(f"{elevator_name}: HTML parse error: {e}")
    return bids


# ---------------------------------------------------------------------------
# Individual elevator scrapers
# ---------------------------------------------------------------------------

async def scrape_scoular_cbloc(context: BrowserContext) -> list[dict]:
    """
    Scoular CBLOC — cookie-first authentication.

    Primary method (SCOULAR_COOKIES set):
      Log in manually once using get_scoular_cookies.py, paste the JSON output
      as the SCOULAR_COOKIES GitHub secret.  No MFA required on subsequent runs.
      Refresh the secret when cookies expire (typically every 30–90 days).

    Fallback method (SCOULAR_USER + SCOULAR_PASS set, no cookies):
      Attempts username/password login.  If Scoular demands an MFA code the
      run will be skipped with a clear error — refresh SCOULAR_COOKIES instead.
    """
    elevator_name = "Scoular CBLOC"
    url = "https://scoularview.com/cbloc-1941"

    cookies_json = os.environ.get("SCOULAR_COOKIES", "")
    username = os.environ.get("SCOULAR_USER", "")
    password = os.environ.get("SCOULAR_PASS", "")

    if not cookies_json and not (username and password):
        logger.warning(f"{elevator_name}: no credentials configured (set SCOULAR_COOKIES); skipping")
        return [], []

    page = await _new_page(context, timeout=60_000)
    try:
        # ── Method 1: inject saved session cookies ───────────────────────────────────────
        if cookies_json:
            try:
                cookies = json.loads(cookies_json)
                await context.add_cookies(cookies)
                logger.info(f"{elevator_name}: injected {len(cookies)} saved cookies")
            except (json.JSONDecodeError, Exception) as exc:
                logger.error(f"{elevator_name}: failed to parse SCOULAR_COOKIES — {exc}")
                return [], []

            await page.goto(url, wait_until="networkidle", timeout=60_000)

            # Check whether we landed on the bids page or got bounced to login.
            # Scoular auth routes through Bushel (app.bushelfarm.com / grain.bushel.ag),
            # so an expired session redirects to a Bushel domain, not just scoularview.com.
            bounced = any(kw in page.url.lower() for kw in ("login", "signin", "auth", "bushel"))
            if bounced and "scoularview.com" not in page.url.lower():
                logger.error(
                    f"{elevator_name}: cookies appear expired (landed on {page.url}) — "
                    "re-run get_scoular_cookies.py locally and update SCOULAR_COOKIES"
                )
                return [], []

            logger.info(f"{elevator_name}: cookie login succeeded; on {page.url}")

        # ── Method 2: username + password (no MFA support) ─────────────────────
        else:
            logger.info(f"{elevator_name}: no cookies; attempting username/password login")
            await page.goto(url, wait_until="networkidle", timeout=60_000)

            user_field = await page.query_selector(
                "input[name='username'], input[name='email'], input[type='email'], "
                "input[id*='user'], input[placeholder*='Username'], input[placeholder*='Email']"
            )
            pass_field = await page.query_selector(
                "input[name='password'], input[type='password'], input[id*='pass']"
            )

            if not user_field or not pass_field:
                logger.warning(f"{elevator_name}: login fields not found on page")
            else:
                await user_field.fill(username)
                await pass_field.fill(password)
                submit = await page.query_selector(
                    "button[type='submit'], input[type='submit'], "
                    "button[class*='login'], button[class*='sign-in']"
                )
                if submit:
                    await submit.click()
                else:
                    await pass_field.press("Enter")
                await page.wait_for_load_state("networkidle", timeout=60_000)

            # If an MFA field appeared we cannot proceed without the cookie method
            mfa_present = await page.query_selector(
                "input[name='code'], input[name='otp'], input[name='token'], "
                "input[maxlength='6'], input[placeholder*='code' i], input[placeholder*='security' i]"
            )
            if mfa_present:
                logger.error(
                    f"{elevator_name}: MFA prompt detected — run get_scoular_cookies.py locally, "
                    "log in with the security code, and store the output as SCOULAR_COOKIES"
                )
                return [], []

            if url not in page.url:
                await page.goto(url, wait_until="networkidle", timeout=60_000)

        # ── Extract bids ───────────────────────────────────────────────────────────────────────
        bids = await _parse_barchart_html(page, elevator_name)
        if not bids:
            rows = await page.query_selector_all("tr, .bid-row, .market-row")
            for row in rows:
                text = await row.inner_text()
                cells = text.split("\t") if "\t" in text else text.split("\n")
                cells = [c.strip() for c in cells if c.strip()]
                if len(cells) >= 3:
                    commodity = cells[0]
                    if any(c in commodity.lower() for c in ["corn", "soybean", "wheat", "beans"]):
                        cash_raw = next((c for c in cells[1:] if re.search(r"\d+\.\d{2}", c)), "")
                        basis_raw = next((c for c in cells[1:] if re.search(r"^[+\-]?\d+$", c)), "")
                        bids.append({
                            "elevator": elevator_name,
                            "commodity": commodity,
                            "delivery_period": cells[1] if len(cells) > 1 else "",
                            "cash_price": _parse_price(cash_raw),
                            "basis": _parse_basis(basis_raw),
                            "futures_reference": "",
                            "change": None,
                        })

        # ── Capture fresh cookies for auto-rotation ─────────────────────────────────
        # After a successful page visit the server may have issued refreshed
        # session cookies (sliding expiry).  We capture them so the orchestrator
        # can write them back to the GitHub secret, keeping the session alive
        # indefinitely without any manual intervention.
        fresh_cookies: list[dict] = []
        if bids:
            try:
                # Capture all cookies — Scoular auth runs through Bushel (bushel.ag),
                # so we need both Bushel and Scoularview cookies for the session to work.
                fresh_cookies = await context.cookies()
                logger.info(f"{elevator_name}: captured {len(fresh_cookies)} fresh cookies for rotation")
            except Exception as exc:
                logger.debug(f"{elevator_name}: could not capture fresh cookies: {exc}")

        logger.info(f"{elevator_name}: extracted {len(bids)} bids")
        return bids, fresh_cookies

    except Exception as exc:
        logger.error(f"{elevator_name}: error — {exc}", exc_info=True)
        return [], []
    finally:
        await page.close()


async def scrape_cargill_east_st_louis(context: BrowserContext) -> list[dict]:
    """Cargill East St. Louis — Barchart widget, public."""
    elevator_name = "Cargill East St. Louis"
    url = "https://www.cargillag.com/locations/east-st-louis-cah"
    page = await _new_page(context)
    try:
        logger.info(f"{elevator_name}: fetching {url}")
        bids = await _fetch_barchart_bids(page, url, elevator_name)
        logger.info(f"{elevator_name}: {len(bids)} bids")
        return bids
    except Exception as e:
        logger.error(f"{elevator_name}: error: {e}", exc_info=True)
        return []
    finally:
        await page.close()


async def scrape_bunge_fairmount_city(context: BrowserContext) -> list[dict]:
    """Bunge Fairmount City — Barchart widget, public."""
    elevator_name = "Bunge Fairmount City"
    url = "https://www.bungeag.com/locations/fairmontcity-il/"
    page = await _new_page(context)
    try:
        logger.info(f"{elevator_name}: fetching {url}")
        bids = await _fetch_barchart_bids(page, url, elevator_name)
        logger.info(f"{elevator_name}: {len(bids)} bids")
        return bids
    except Exception as e:
        logger.error(f"{elevator_name}: error: {e}", exc_info=True)
        return []
    finally:
        await page.close()


async def scrape_bartlett_jacksonville(context: BrowserContext) -> list[dict]:
    """Bartlett Jacksonville — public Barchart-style site."""
    elevator_name = "Bartlett Jacksonville"
    url = "https://bartlettco.com/locations/il/jacksonville/"
    page = await _new_page(context)
    try:
        logger.info(f"{elevator_name}: fetching {url}")
        bids = await _fetch_barchart_bids(page, url, elevator_name)
        logger.info(f"{elevator_name}: {len(bids)} bids")
        return bids
    except Exception as e:
        logger.error(f"{elevator_name}: error: {e}", exc_info=True)
        return []
    finally:
        await page.close()


async def scrape_adm_gradable(
    context: BrowserContext,
    url: str,
    elevator_name: str,
) -> list[dict]:
    """
    ADM Gradable React app scraper (shared logic for Decatur Soy, Decatur Corn, Sauget).
    Waits for React to render bid cards/table then extracts all bids shown.
    """
    page = await _new_page(context)
    bids: list[dict] = []
    try:
        logger.info(f"{elevator_name}: fetching {url}")

        # Intercept API calls that the React app makes for bid data
        api_bids: list[dict] = []
        api_hit = False

        async def handle_response(response):
            nonlocal api_hit
            try:
                rurl = response.url
                if any(kw in rurl for kw in ["bids", "cash", "market", "prices", "gradable"]):
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        body = await response.json()
                        api_hit = True
                        # Extract from various possible response shapes
                        items = []
                        if isinstance(body, list):
                            items = body
                        elif isinstance(body, dict):
                            for key in ["bids", "data", "results", "cashBids", "items"]:
                                if isinstance(body.get(key), list):
                                    items = body[key]
                                    break
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            commodity = item.get("commodity", item.get("commodityName", item.get("name", "")))
                            delivery = item.get("deliveryPeriod", item.get("delivery", item.get("period", item.get("contractName", ""))))
                            cash = item.get("cashPrice", item.get("price", item.get("bidPrice", None)))
                            basis = item.get("basis", item.get("basisPrice", None))
                            futures_ref = item.get("futuresCode", item.get("futuresSymbol", item.get("contract", "")))
                            change = item.get("change", item.get("netChange", None))
                            if commodity:
                                api_bids.append({
                                    "elevator": elevator_name,
                                    "commodity": str(commodity),
                                    "delivery_period": str(delivery) if delivery else "",
                                    "cash_price": float(cash) if cash is not None else None,
                                    "basis": float(basis) if basis is not None else None,
                                    "futures_reference": str(futures_ref) if futures_ref else "",
                                    "change": float(change) if change is not None else None,
                                })
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: navigation warning: {e}")

        # Wait for React to render — try common selectors
        react_selectors = [
            ".bid-card",
            ".bids-table",
            "table[class*='bid']",
            "[class*='BidTable']",
            "[class*='bidRow']",
            "[class*='bid-row']",
            "[data-testid*='bid']",
            "table tbody tr",
        ]
        for sel in react_selectors:
            try:
                await page.wait_for_selector(sel, timeout=10_000)
                break
            except Exception:
                continue

        # Give extra time for React rendering
        await asyncio.sleep(3)

        if api_hit and api_bids:
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API intercept")
            return api_bids

        # Fallback: scrape rendered HTML
        # ADM Gradable typically renders a table or card-based layout
        bids = await _parse_adm_gradable_html(page, elevator_name)
        logger.info(f"{elevator_name}: {len(bids)} bids from HTML parse")
        return bids

    except Exception as e:
        logger.error(f"{elevator_name}: error: {e}", exc_info=True)
        return []
    finally:
        await page.close()


async def _parse_adm_gradable_html(page: Page, elevator_name: str) -> list[dict]:
    """Parse ADM Gradable React-rendered HTML."""
    bids = []
    try:
        # Try table first
        table = await page.query_selector("table")
        if table:
            rows = await table.query_selector_all("tbody tr, tr")
            for row in rows:
                cells = await row.query_selector_all("td, th")
                vals = [await c.inner_text() for c in cells]
                vals = [v.strip() for v in vals]
                if len(vals) < 3:
                    continue
                # Skip header rows
                if any(h in vals[0].lower() for h in ["commodity", "grain", "bid", "delivery", "period"]):
                    continue
                commodity = vals[0]
                delivery = vals[1] if len(vals) > 1 else ""
                cash_raw = vals[2] if len(vals) > 2 else ""
                basis_raw = vals[3] if len(vals) > 3 else ""
                futures_raw = vals[4] if len(vals) > 4 else ""
                change_raw = vals[5] if len(vals) > 5 else ""
                cash_price = _parse_price(cash_raw)
                if commodity and cash_price is not None:
                    bids.append({
                        "elevator": elevator_name,
                        "commodity": commodity,
                        "delivery_period": delivery,
                        "cash_price": cash_price,
                        "basis": _parse_basis(basis_raw),
                        "futures_reference": futures_raw,
                        "change": _parse_price(change_raw),
                    })
            if bids:
                return bids

        # Try card-based layout
        cards = await page.query_selector_all(
            ".bid-card, [class*='bidCard'], [class*='BidCard'], [class*='bid-row'], [class*='BidRow']"
        )
        for card in cards:
            text = await card.inner_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            # Attempt to extract commodity, delivery, cash, basis from lines
            commodity = ""
            delivery = ""
            cash_price = None
            basis = None
            for line in lines:
                if any(c in line.lower() for c in ["corn", "soybean", "wheat", "beans"]):
                    commodity = line
                elif re.match(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}", line):
                    delivery = line
                elif re.match(r"^\$?\d+\.\d+$", line):
                    if cash_price is None:
                        cash_price = _parse_price(line)
                elif re.match(r"^[+\-]\d+(\.\d+)?$", line):
                    basis = _parse_basis(line)
            if commodity and cash_price is not None:
                bids.append({
                    "elevator": elevator_name,
                    "commodity": commodity,
                    "delivery_period": delivery,
                    "cash_price": cash_price,
                    "basis": basis,
                    "futures_reference": "",
                    "change": None,
                })
    except Exception as e:
        logger.error(f"{elevator_name}: HTML parse error: {e}")
    return bids


async def scrape_adm_decatur_soy(context: BrowserContext) -> list[dict]:
    """ADM Decatur Soy Processing."""
    return await scrape_adm_gradable(
        context,
        "https://adm.gradable.com/my-sales-lite/bids/Decatur--IL-Soy-Processing",
        "ADM Decatur Soy",
    )


async def scrape_adm_decatur_corn(context: BrowserContext) -> list[dict]:
    """ADM Decatur Corn Processing."""
    return await scrape_adm_gradable(
        context,
        "https://adm.gradable.com/my-sales-lite/bids/Decatur--IL-Corn-Processing",
        "ADM Decatur Corn",
    )


async def scrape_adm_sauget(context: BrowserContext) -> list[dict]:
    """ADM Sauget, IL."""
    return await scrape_adm_gradable(
        context,
        "https://adm.gradable.com/my-sales-lite/bids/Sauget--IL",
        "ADM Sauget",
    )


async def scrape_chs_illinois(context: BrowserContext) -> list[dict]:
    """
    CHS Illinois — public cash bids page.
    Extracts Lowder and Cahokia location bids only.
    """
    elevator_name = "CHS Illinois"
    url = "https://www.chs-illinois.com/grain/cash-bids/"
    target_locations = ["lowder", "cahokia"]
    page = await _new_page(context)
    bids: list[dict] = []

    try:
        logger.info(f"{elevator_name}: fetching {url}")

        # Network intercept for API calls
        api_bids: list[dict] = []
        api_hit = False

        async def handle_response(response):
            nonlocal api_hit
            try:
                rurl = response.url.lower()
                if any(kw in rurl for kw in ["cashbid", "cash_bid", "bids", "market"]):
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        body = await response.json()
                        api_hit = True
                        items = []
                        if isinstance(body, list):
                            items = body
                        elif isinstance(body, dict):
                            for key in ["bids", "data", "results", "cashBids"]:
                                if isinstance(body.get(key), list):
                                    items = body[key]
                                    break
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            loc = str(item.get("location", item.get("locationName", item.get("elevator", "")))).lower()
                            if not any(tl in loc for tl in target_locations):
                                continue
                            commodity = item.get("commodity", item.get("name", ""))
                            api_bids.append({
                                "elevator": f"CHS {loc.title()}",
                                "commodity": str(commodity),
                                "delivery_period": str(item.get("deliveryPeriod", item.get("period", ""))),
                                "cash_price": float(item["cashPrice"]) if item.get("cashPrice") is not None else None,
                                "basis": float(item["basis"]) if item.get("basis") is not None else None,
                                "futures_reference": str(item.get("futuresCode", "")),
                                "change": float(item["change"]) if item.get("change") is not None else None,
                            })
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: nav warning: {e}")

        await asyncio.sleep(3)

        if api_hit and api_bids:
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API (Lowder+Cahokia)")
            return api_bids

        # HTML fallback: look for location sections
        bids = await _parse_chs_html(page, elevator_name, target_locations)
        logger.info(f"{elevator_name}: {len(bids)} bids from HTML parse")
        return bids

    except Exception as e:
        logger.error(f"{elevator_name}: error: {e}", exc_info=True)
        return []
    finally:
        await page.close()


async def _parse_chs_html(page: Page, elevator_name: str, target_locations: list[str]) -> list[dict]:
    """Parse CHS Illinois HTML, filtering for target locations."""
    bids = []
    try:
        # Try to find location sections/headers
        content = await page.content()
        # Look for section headers containing location names
        sections = await page.query_selector_all(
            "section, .location-section, [class*='location'], [class*='elevator'], h2, h3, h4"
        )

        current_location = None
        for elem in sections:
            text = await elem.inner_text()
            text_lower = text.lower().strip()
            if any(tl in text_lower for tl in target_locations):
                current_location = text.strip()
                # Get the table after this header
                table = await elem.evaluate_handle(
                    "(el) => el.nextElementSibling"
                )
                if table:
                    table_rows = await table.query_selector_all("tr")
                    for row in table_rows:
                        cells = await row.query_selector_all("td")
                        vals = [await c.inner_text() for c in cells]
                        vals = [v.strip() for v in vals]
                        if len(vals) < 3:
                            continue
                        commodity = vals[0]
                        delivery = vals[1] if len(vals) > 1 else ""
                        cash_raw = vals[2] if len(vals) > 2 else ""
                        basis_raw = vals[3] if len(vals) > 3 else ""
                        futures_raw = vals[4] if len(vals) > 4 else ""
                        cash_price = _parse_price(cash_raw)
                        if commodity and cash_price is not None:
                            bids.append({
                                "elevator": f"CHS {current_location}",
                                "commodity": commodity,
                                "delivery_period": delivery,
                                "cash_price": cash_price,
                                "basis": _parse_basis(basis_raw),
                                "futures_reference": futures_raw,
                                "change": None,
                            })

        if not bids:
            # Try Barchart-style parsing as fallback
            bids = await _parse_barchart_html(page, elevator_name)
            # Filter by location in bid text if possible
    except Exception as e:
        logger.error(f"{elevator_name}: HTML parse error: {e}")
    return bids


async def scrape_gpre_madison(context: BrowserContext) -> list[dict]:
    """
    Green Plains (GPRe) corn bids page — filter for Madison, IL location.
    """
    elevator_name = "GPRe Madison"
    url = "https://gpreinc.com/corn-bids/"
    target_location = "madison"
    page = await _new_page(context)
    bids: list[dict] = []

    try:
        logger.info(f"{elevator_name}: fetching {url}")

        api_bids: list[dict] = []
        api_hit = False

        async def handle_response(response):
            nonlocal api_hit
            try:
                rurl = response.url.lower()
                if any(kw in rurl for kw in ["bid", "cash", "corn", "market", "price"]):
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        body = await response.json()
                        api_hit = True
                        items = []
                        if isinstance(body, list):
                            items = body
                        elif isinstance(body, dict):
                            for key in ["bids", "data", "results", "cashBids", "locations"]:
                                if isinstance(body.get(key), list):
                                    items = body[key]
                                    break
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            loc = str(item.get("location", item.get("city", item.get("name", "")))).lower()
                            if target_location not in loc:
                                continue
                            commodity = item.get("commodity", "Corn")
                            api_bids.append({
                                "elevator": elevator_name,
                                "commodity": str(commodity),
                                "delivery_period": str(item.get("deliveryPeriod", item.get("period", item.get("month", "")))),
                                "cash_price": float(item["cashPrice"]) if item.get("cashPrice") is not None else None,
                                "basis": float(item["basis"]) if item.get("basis") is not None else None,
                                "futures_reference": str(item.get("futuresCode", "")),
                                "change": float(item["change"]) if item.get("change") is not None else None,
                            })
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: nav warning: {e}")

        await asyncio.sleep(3)

        if api_hit and api_bids:
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API (Madison IL)")
            return api_bids

        # HTML fallback
        bids = await _parse_gpre_html(page, elevator_name, target_location)
        logger.info(f"{elevator_name}: {len(bids)} bids from HTML parse")
        return bids

    except Exception as e:
        logger.error(f"{elevator_name}: error: {e}", exc_info=True)
        return []
    finally:
        await page.close()


async def _parse_gpre_html(page: Page, elevator_name: str, target_location: str) -> list[dict]:
    """Parse GPRe HTML for Madison IL bids."""
    bids = []
    try:
        # Find the Madison section
        all_text = await page.evaluate("document.body.innerText")
        # Check if Madison is even mentioned
        if target_location.lower() not in all_text.lower():
            logger.warning(f"{elevator_name}: '{target_location}' not found on page")
            return bids

        # Try to find Madison-specific table
        tables = await page.query_selector_all("table")
        for table in tables:
            # Check parent/surrounding context for Madison label
            parent_text = await table.evaluate("(el) => { let p = el.parentElement; return p ? p.innerText : ''; }")
            if target_location.lower() in parent_text.lower():
                rows = await table.query_selector_all("tr")
                for row in rows:
                    cells = await row.query_selector_all("td")
                    vals = [await c.inner_text() for c in cells]
                    vals = [v.strip() for v in vals]
                    if len(vals) < 3:
                        continue
                    commodity = vals[0]
                    delivery = vals[1] if len(vals) > 1 else ""
                    cash_raw = vals[2] if len(vals) > 2 else ""
                    basis_raw = vals[3] if len(vals) > 3 else ""
                    cash_price = _parse_price(cash_raw)
                    if commodity and cash_price is not None:
                        bids.append({
                            "elevator": elevator_name,
                            "commodity": commodity,
                            "delivery_period": delivery,
                            "cash_price": cash_price,
                            "basis": _parse_basis(basis_raw),
                            "futures_reference": "",
                            "change": None,
                        })
                break

        if not bids:
            # Fallback: parse all bids and note they're from GPRe
            bids = await _parse_barchart_html(page, elevator_name)
            # Label with Madison if we couldn't filter
    except Exception as e:
        logger.error(f"{elevator_name}: HTML parse error: {e}")
    return bids


async def scrape_cgb(context: BrowserContext) -> list[dict]:
    """CGB Grain — public Barchart-style table, location 20605."""
    elevator_name = "CGB"
    url = "https://www.cgbgrain.com/Market-Solutions/Cash-Bids?format=table&groupby=location&setLocation=20605&commodity="
    page = await _new_page(context)
    try:
        logger.info(f"{elevator_name}: fetching {url}")
        bids = await _fetch_barchart_bids(page, url, elevator_name)
        logger.info(f"{elevator_name}: {len(bids)} bids")
        return bids
    except Exception as e:
        logger.error(f"{elevator_name}: error: {e}", exc_info=True)
        return []
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Master scraping function
# ---------------------------------------------------------------------------

async def fetch_all_elevator_bids() -> tuple[dict[str, list[dict]], list[dict]]:
    """
    Run all elevator scrapers concurrently.

    Returns:
        (bids_by_elevator, scoular_fresh_cookies)

        bids_by_elevator  — dict keyed by elevator name, value is list of bid dicts
        scoular_fresh_cookies — cookies captured after a successful Scoular login,
                                ready to be written back to SCOULAR_COOKIES secret;
                                empty list if Scoular was skipped or failed
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        # Scoular is run separately (returns a tuple); all others return list[dict]
        non_scoular_tasks = {
            "Cargill East St. Louis": scrape_cargill_east_st_louis(context),
            "Bunge Fairmount City": scrape_bunge_fairmount_city(context),
            "Bartlett Jacksonville": scrape_bartlett_jacksonville(context),
            "ADM Decatur Soy": scrape_adm_decatur_soy(context),
            "ADM Decatur Corn": scrape_adm_decatur_corn(context),
            "ADM Sauget": scrape_adm_sauget(context),
            "CHS Illinois": scrape_chs_illinois(context),
            "GPRe Madison": scrape_gpre_madison(context),
            "CGB": scrape_cgb(context),
        }

        scoular_task = scrape_scoular_cbloc(context)

        scoular_result, *other_results = await asyncio.gather(
            scoular_task,
            *non_scoular_tasks.values(),
            return_exceptions=True,
        )

        await context.close()
        await browser.close()

    # Unpack Scoular (bids, fresh_cookies)
    output: dict[str, list[dict]] = {}
    scoular_fresh_cookies: list[dict] = []
    if isinstance(scoular_result, Exception):
        logger.error(f"Scoular CBLOC: unhandled exception: {scoular_result}")
        output["Scoular CBLOC"] = []
    else:
        scoular_bids, scoular_fresh_cookies = scoular_result
        output["Scoular CBLOC"] = scoular_bids

    # Unpack remaining elevators
    for name, result in zip(non_scoular_tasks.keys(), other_results):
        if isinstance(result, Exception):
            logger.error(f"{name}: unhandled exception: {result}")
            output[name] = []
        else:
            output[name] = result

    total_bids = sum(len(v) for v in output.values())
    logger.info(f"Elevator scraping complete: {total_bids} total bids from {len(output)} elevators")
    return output, scoular_fresh_cookies
