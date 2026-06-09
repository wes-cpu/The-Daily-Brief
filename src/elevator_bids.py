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
    """Parse a price string including CBOT fraction format ('440-2' = 440.25¢/bu)."""
    if not raw:
        return None
    raw = raw.strip()
    # CBOT fraction format: whole-eighths, e.g. "440-2" = 440 + 2/8 = 440.25
    m = re.match(r'^(\d+)-(\d+)$', raw)
    if m:
        return float(m.group(1)) + float(m.group(2)) / 8.0
    cleaned = re.sub(r"[^\d.\-]", "", raw)
    try:
        val = float(cleaned)
        # Reject values that look like dates (e.g. 6302026 from "06/30/2026")
        if val > 99_999:
            return None
        return val
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

def _parse_barchart_api_body(body: dict, elevator_name: str) -> list[dict]:
    """Parse a Barchart WebSol JSON response body into bid dicts."""
    bids_data = (
        (body.get("data", {}).get("cashBids", []) if isinstance(body.get("data"), dict) else [])
        or body.get("cashBids", [])
        or body.get("results", [])
        or (body.get("data", []) if isinstance(body.get("data"), list) else [])
    )
    bids = []
    for item in bids_data:
        if not isinstance(item, dict):
            continue
        commodity = item.get("commodity", item.get("name", "")).strip()
        if not commodity:
            continue
        bids.append({
            "elevator": elevator_name,
            "commodity": commodity,
            "delivery_period": item.get("expirationDate", item.get("deliveryPeriod", "")),
            "cash_price": _parse_price(str(item.get("cashPrice", item.get("price", "")))),
            "basis": _parse_basis(str(item.get("basis", ""))),
            "futures_reference": item.get("futuresCode", item.get("contractCode", "")),
            "change": _parse_price(str(item.get("netChange", item.get("change", "")))),
        })
    return bids


async def _fetch_barchart_bids(
    page: Page,
    url: str,
    elevator_name: str,
) -> list[dict]:
    """
    Fetch Barchart WebSol cash bids using page.route() + route.fetch().

    page.route intercepts the request BEFORE the browser JS can consume the
    response body, which is why the older page.on("response") approach silently
    failed — by the time our handler ran, the body was already consumed.
    """
    captured_bids: list[dict] = []
    api_captured = False

    # URL substrings that identify Barchart WebSol API calls
    BARCHART_HINTS = (
        "getCashBids", "getGrainBids",
        "websol.barchart.com", "ondemand.websol",
        "cfwidget.barchart",
    )

    async def intercept_barchart(route):
        nonlocal api_captured
        req_url = route.request.url
        if not any(h in req_url for h in BARCHART_HINTS):
            await route.continue_()
            return
        try:
            response = await route.fetch()
            ct = response.headers.get("content-type", "")
            if "html" not in ct:
                try:
                    raw = await response.body()
                    body = json.loads(raw)
                    new_bids = _parse_barchart_api_body(body, elevator_name)
                    if new_bids:
                        captured_bids.extend(new_bids)
                        api_captured = True
                        logger.info(
                            f"{elevator_name}: {len(new_bids)} bids via route intercept "
                            f"({req_url[:80]})"
                        )
                except Exception as e:
                    logger.debug(f"{elevator_name}: route body parse error for {req_url}: {e}")
            await route.fulfill(response=response)
        except Exception as e:
            logger.debug(f"{elevator_name}: route fetch error: {e}")
            await route.continue_()

    await page.route("**/*", intercept_barchart)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        logger.warning(f"{elevator_name}: navigation error: {e}")

    # Scroll to trigger lazy-loaded Barchart widgets (IntersectionObserver)
    try:
        await page.evaluate("() => window.scrollTo(0, Math.min(600, document.body.scrollHeight))")
    except Exception:
        pass

    # Poll up to 35 s for the widget API call
    for _ in range(7):
        if api_captured:
            break
        await asyncio.sleep(5)

    await page.unroute("**/*")

    if api_captured and captured_bids:
        return captured_bids

    logger.info(f"{elevator_name}: route intercept missed; trying HTML parse")
    return await _parse_barchart_html(page, elevator_name)


async def _parse_barchart_html(page: Page, elevator_name: str) -> list[dict]:
    """
    Parse rendered Barchart cash-bid data from page HTML.

    Handles both the older table-based layout and the newer div/card layout
    used by Barchart WebSol v2/v3 widgets.
    """
    bids = []
    try:
        # ── Approach 1: div/card-based Barchart v2/v3 widget ────────────────
        # These widgets render rows as divs rather than <tr>/<td>.
        div_row_selectors = [
            ".bc-cash-bids tbody tr",
            "[class*='cashBid'] tr",
            "[class*='cash-bid'] tr",
            "[data-module='CashBids'] tr",
            ".bc-cash-bids [class*='row']",
            "[class*='CashBids'] [class*='row']",
            "[class*='cashbid'] [class*='row']",
        ]
        rows_found = []
        for sel in div_row_selectors:
            try:
                rows_found = await page.query_selector_all(sel)
                if rows_found:
                    logger.debug(f"{elevator_name}: HTML parse using selector '{sel}'")
                    break
            except Exception:
                continue

        if rows_found:
            for row in rows_found:
                cells = await row.query_selector_all("td, [class*='cell'], [class*='col']")
                vals = [await c.inner_text() for c in cells]
                vals = [v.strip() for v in vals if v.strip()]
                if len(vals) < 3:
                    continue
                commodity = vals[0]
                delivery  = vals[1] if len(vals) > 1 else ""
                cash_raw  = vals[2] if len(vals) > 2 else ""
                basis_raw = vals[3] if len(vals) > 3 else ""
                futures_ref = vals[4] if len(vals) > 4 else ""
                change_raw  = vals[5] if len(vals) > 5 else ""
                if any(h in commodity.lower() for h in ["commodity", "grain", "crop", "description"]):
                    continue
                cash_price = _parse_price(cash_raw)
                if commodity and cash_price is not None:
                    bids.append({
                        "elevator": elevator_name,
                        "commodity": commodity,
                        "delivery_period": delivery,
                        "cash_price": cash_price,
                        "basis": _parse_basis(basis_raw),
                        "futures_reference": futures_ref,
                        "change": _parse_price(change_raw),
                    })
            if bids:
                return bids

        # ── Approach 2: classic <table> ─────────────────────────────────────
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
            logger.warning(f"{elevator_name}: no table or bid widget found in HTML fallback")
            return bids

        rows = await table.query_selector_all("tr")
        header_row = rows[0] if rows else None
        headers = []
        if header_row:
            ths = await header_row.query_selector_all("th, td")
            headers = [await th.inner_text() for th in ths]
            headers = [h.strip().lower() for h in headers]

        col = {}
        for i, h in enumerate(headers):
            if any(k in h for k in ("commodity", "grain", "crop")):
                col.setdefault("commodity", i)
            elif any(k in h for k in ("delivery", "period", "month", "start")):
                col.setdefault("delivery", i)
            elif any(k in h for k in ("cash", "bid", "price")) and "futures" not in h:
                col.setdefault("cash", i)
            elif "basis" in h:
                col.setdefault("basis", i)
            elif any(k in h for k in ("futures", "contract", "symbol")):
                col.setdefault("futures_ref", i)
            elif any(k in h for k in ("change", "chg")):
                col.setdefault("change", i)

        def _get(vals, key, default_idx):
            idx = col.get(key, default_idx)
            return vals[idx] if idx < len(vals) else ""

        for row in rows[1:]:
            cells = await row.query_selector_all("td")
            if not cells:
                continue
            vals = [await c.inner_text() for c in cells]
            vals = [v.strip() for v in vals]
            commodity = _get(vals, "commodity", 0)
            delivery  = _get(vals, "delivery",  1)
            cash_raw  = _get(vals, "cash",      2)
            basis_raw = _get(vals, "basis",     3)
            futures_ref = _get(vals, "futures_ref", 4)
            change_raw  = _get(vals, "change",      5)
            cash_price = _parse_price(cash_raw)
            if commodity and cash_price is not None:
                bids.append({
                    "elevator": elevator_name,
                    "commodity": commodity,
                    "delivery_period": delivery,
                    "cash_price": cash_price,
                    "basis": _parse_basis(basis_raw),
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

    Uses page.route() interception to guarantee response body availability before
    the React app's fetch() handler consumes it.
    """
    page = await _new_page(context)
    api_bids: list[dict] = []
    api_hit = False

    # URL substrings that suggest the React app's bid-data API calls
    ADM_HINTS = (
        "gradable", "/bids", "/bid", "/cash", "/market",
        "/prices", "/commodity", "/api/", "telus",
    )

    async def intercept_adm(route):
        nonlocal api_hit
        req_url = route.request.url
        # Only intercept likely data API calls (skip static assets)
        if any(req_url.endswith(ext) for ext in (".js", ".css", ".woff2", ".woff", ".png", ".jpg", ".ico", ".svg")):
            await route.continue_()
            return
        if not any(h in req_url for h in ADM_HINTS):
            await route.continue_()
            return
        try:
            response = await route.fetch()
            ct = response.headers.get("content-type", "")
            if "json" in ct or "json" in req_url:
                try:
                    raw = await response.body()
                    body = json.loads(raw)
                    items = []
                    if isinstance(body, list):
                        items = body
                    elif isinstance(body, dict):
                        for key in ["bids", "data", "results", "cashBids", "items", "listings"]:
                            if isinstance(body.get(key), list):
                                items = body[key]
                                break
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        commodity = item.get("commodity", item.get("commodityName", item.get("name", "")))
                        if not commodity:
                            continue
                        delivery = item.get("deliveryPeriod", item.get("delivery", item.get("period", item.get("contractName", ""))))
                        cash = item.get("cashPrice", item.get("price", item.get("bidPrice")))
                        basis = item.get("basis", item.get("basisPrice"))
                        futures_ref = item.get("futuresCode", item.get("futuresSymbol", item.get("contract", "")))
                        change = item.get("change", item.get("netChange"))
                        api_bids.append({
                            "elevator": elevator_name,
                            "commodity": str(commodity),
                            "delivery_period": str(delivery) if delivery else "",
                            "cash_price": float(cash) if cash is not None else None,
                            "basis": float(basis) if basis is not None else None,
                            "futures_reference": str(futures_ref) if futures_ref else "",
                            "change": float(change) if change is not None else None,
                        })
                    if items:
                        api_hit = True
                        logger.info(f"{elevator_name}: {len(api_bids)} bids via route intercept ({req_url[:80]})")
                except Exception as e:
                    logger.debug(f"{elevator_name}: ADM route parse error for {req_url}: {e}")
            await route.fulfill(response=response)
        except Exception as e:
            logger.debug(f"{elevator_name}: ADM route error: {e}")
            await route.continue_()

    try:
        logger.info(f"{elevator_name}: fetching {url}")
        await page.route("**/*", intercept_adm)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: navigation warning: {e}")

        # Scroll to trigger any lazy loading
        try:
            await page.evaluate("() => window.scrollTo(0, 400)")
        except Exception:
            pass

        # Wait up to 25 s for the React app to fetch its data
        for _ in range(5):
            if api_hit:
                break
            await asyncio.sleep(5)

        await page.unroute("**/*")

        if api_hit and api_bids:
            return [b for b in api_bids if b.get("cash_price") is not None or b.get("basis") is not None]

        # Fallback: scrape rendered HTML
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
    Uses page.route() interception for reliable response body access.
    """
    elevator_name = "CHS Illinois"
    url = "https://www.chs-illinois.com/grain/cash-bids/"
    target_locations = ["lowder", "cahokia"]
    page = await _new_page(context)
    api_bids: list[dict] = []
    api_hit = False

    CHS_HINTS = ("cashbid", "cash_bid", "bids", "market", "grain", "websol", "barchart")

    async def intercept_chs(route):
        nonlocal api_hit
        req_url = route.request.url.lower()
        if any(req_url.endswith(ext) for ext in (".js", ".css", ".woff2", ".woff", ".png", ".jpg", ".ico", ".svg")):
            await route.continue_()
            return
        if not any(h in req_url for h in CHS_HINTS):
            await route.continue_()
            return
        try:
            response = await route.fetch()
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                raw = await response.body()
                body = json.loads(raw)
                items = []
                if isinstance(body, list):
                    items = body
                elif isinstance(body, dict):
                    for key in ["bids", "data", "results", "cashBids"]:
                        if isinstance(body.get(key), list):
                            items = body[key]
                            break
                # Try Barchart-style body first (handles getCashBids response shape)
                bc_bids = _parse_barchart_api_body(body, elevator_name)
                if bc_bids:
                    api_bids.extend(bc_bids)
                    api_hit = True
                    logger.info(f"{elevator_name}: {len(bc_bids)} bids via Barchart route ({req_url[:60]})")
                elif items:
                    # Location-keyed response — collect all, filter to targets later
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        loc = str(item.get("location", item.get("locationName", item.get("elevator", "")))).lower()
                        commodity = item.get("commodity", item.get("name", ""))
                        if not commodity:
                            continue
                        elev_label = f"CHS {loc.title()}" if loc else elevator_name
                        api_bids.append({
                            "elevator": elev_label,
                            "commodity": str(commodity),
                            "delivery_period": str(item.get("deliveryPeriod", item.get("period", ""))),
                            "cash_price": float(item["cashPrice"]) if item.get("cashPrice") is not None else None,
                            "basis": float(item["basis"]) if item.get("basis") is not None else None,
                            "futures_reference": str(item.get("futuresCode", "")),
                            "change": float(item["change"]) if item.get("change") is not None else None,
                        })
                    api_hit = True
                    logger.info(f"{elevator_name}: {len(api_bids)} bids via route ({req_url[:60]})")
            await route.fulfill(response=response)
        except Exception as e:
            logger.debug(f"{elevator_name}: CHS route error: {e}")
            await route.continue_()

    try:
        logger.info(f"{elevator_name}: fetching {url}")
        await page.route("**/*", intercept_chs)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: nav warning: {e}")

        try:
            await page.evaluate("() => window.scrollTo(0, 400)")
        except Exception:
            pass

        for _ in range(5):
            if api_hit:
                break
            await asyncio.sleep(5)

        await page.unroute("**/*")

        if api_hit and api_bids:
            # Filter to target locations if location data is present
            filtered = [b for b in api_bids if any(tl in b["elevator"].lower() for tl in target_locations)]
            if filtered:
                logger.info(f"{elevator_name}: {len(filtered)} bids (Lowder+Cahokia) via API")
                return filtered
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API (all locations)")
            return api_bids

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
    Uses page.route() for reliable API interception.
    """
    elevator_name = "GPRe Madison"
    url = "https://gpreinc.com/corn-bids/"
    target_location = "madison"
    page = await _new_page(context)
    api_bids: list[dict] = []
    api_hit = False

    GPRE_HINTS = ("bid", "cash", "corn", "market", "price", "websol", "barchart", "grain")

    async def intercept_gpre(route):
        nonlocal api_hit
        req_url = route.request.url.lower()
        if any(req_url.endswith(ext) for ext in (".js", ".css", ".woff2", ".woff", ".png", ".jpg", ".ico", ".svg")):
            await route.continue_()
            return
        if not any(h in req_url for h in GPRE_HINTS):
            await route.continue_()
            return
        try:
            response = await route.fetch()
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                raw = await response.body()
                body = json.loads(raw)

                # Try Barchart-style response first
                bc_bids = _parse_barchart_api_body(body, elevator_name)
                if bc_bids:
                    api_bids.extend(bc_bids)
                    api_hit = True
                    logger.info(f"{elevator_name}: {len(bc_bids)} bids via Barchart route ({req_url[:60]})")
                    await route.fulfill(response=response)
                    return

                # Try location-based response
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
                if items:
                    api_hit = True
                    logger.info(f"{elevator_name}: {len(api_bids)} bids via route ({req_url[:60]})")
            await route.fulfill(response=response)
        except Exception as e:
            logger.debug(f"{elevator_name}: GPRe route error: {e}")
            await route.continue_()

    try:
        logger.info(f"{elevator_name}: fetching {url}")
        await page.route("**/*", intercept_gpre)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: nav warning: {e}")

        try:
            await page.evaluate("() => window.scrollTo(0, 400)")
        except Exception:
            pass

        for _ in range(5):
            if api_hit:
                break
            await asyncio.sleep(5)

        await page.unroute("**/*")

        if api_hit and api_bids:
            # Filter to Madison if location data is present
            madison_bids = [b for b in api_bids if target_location in b.get("elevator", "").lower()
                           or target_location in b.get("delivery_period", "").lower()]
            if madison_bids:
                logger.info(f"{elevator_name}: {len(madison_bids)} Madison IL bids via API")
                return madison_bids
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API (unfiltered)")
            return api_bids

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
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        # Hide webdriver flag from bot-detection scripts
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        # Block images and fonts to speed up page loads
        await context.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,webp,woff,woff2,ttf,eot}",
            lambda route: route.abort(),
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
