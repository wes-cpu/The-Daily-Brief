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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30_000  # 30 seconds in ms

# Comprehensive stealth script applied to every page.
# Patches the fingerprinting surfaces that headless-Chrome detection checks.
_STEALTH_SCRIPT = """
// Disable webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Realistic plugins list
const _plugins = [
  {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format',length:1},
  {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'',length:1},
  {name:'Native Client', filename:'internal-nacl-plugin', description:'',length:0},
];
Object.defineProperty(navigator, 'plugins', {get: () => _plugins});
Object.defineProperty(navigator, 'mimeTypes', {get: () => []});

// Language / locale
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'language', {get: () => 'en-US'});

// Hardware hints — match a normal desktop
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// Chrome runtime object expected by Barchart widgets
if (!window.chrome) {
  window.chrome = {
    runtime: {},
    app: {isInstalled: false},
    loadTimes: function() {
      return {
        commitLoadTime: Date.now()/1000 - 0.3,
        connectionInfo: 'http/1.1',
        finishDocumentLoadTime: 0, finishLoadTime: 0,
        firstPaintAfterLoadTime: 0, firstPaintTime: 0,
        navigationType: 'Other', npnNegotiatedProtocol: 'unknown',
        requestTime: Date.now()/1000 - 0.8,
        startLoadTime: Date.now()/1000 - 1.0,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: false, wasNpnNegotiated: false,
      };
    },
    csi: function() {
      return {
        onloadT: Date.now(),
        pageT: Date.now() - performance.timing.navigationStart,
        startE: performance.timing.navigationStart, tran: 15,
      };
    },
  };
}

// Permissions — avoid automated-browser fingerprint
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) =>
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : _origQuery(p);

// Slight canvas noise to break pixel-exact fingerprinting
const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
  const d = _origGetImageData.call(this, x, y, w, h);
  for (let i = 0; i < d.data.length; i += 97) { d.data[i] ^= 1; }
  return d;
};
"""


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
    Poll the inbox at imap_user for a Scoular security-code email,
    retrying every poll_interval seconds for up to wait_seconds total.
    Returns the extracted 6-digit code, or None on failure.
    """
    deadline = time.monotonic() + wait_seconds
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            with imaplib.IMAP4_SSL(imap_host) as mail:
                mail.login(imap_user, imap_pass)
                mail.select("INBOX")

                status, msg_ids = mail.search(
                    None,
                    '(FROM "scoular" SUBJECT "security" UNSEEN)',
                )
                if status != "OK" or not msg_ids[0]:
                    status, msg_ids = mail.search(None, '(FROM "scoular" UNSEEN)')

                if status == "OK" and msg_ids[0]:
                    ids = msg_ids[0].split()
                    raw = mail.fetch(ids[-1], "(RFC822)")[1][0][1]
                    msg = email_lib.message_from_bytes(raw)

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(errors="replace")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="replace")

                    match = re.search(r"\b(\d{6})\b", body)
                    if match:
                        code = match.group(1)
                        logger.info(f"Scoular MFA: found code {code} (attempt {attempt})")
                        mail.store(ids[-1], "+FLAGS", "\\Seen")
                        return code
                    else:
                        logger.debug(f"Scoular MFA: email found but no 6-digit code (attempt {attempt})")
                else:
                    logger.debug(f"Scoular MFA: no matching email yet (attempt {attempt})")

        except imaplib.IMAP4.error as exc:
            logger.error(f"Scoular MFA IMAP error: {exc}")
            return None

        remaining = deadline - time.monotonic()
        if remaining > 0:
            sleep_for = min(poll_interval, remaining)
            logger.info(f"Scoular MFA: waiting {sleep_for:.0f}s… ({remaining:.0f}s left)")
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
    m = re.match(r'^(\d+)-(\d+)$', raw)
    if m:
        return float(m.group(1)) + float(m.group(2)) / 8.0
    cleaned = re.sub(r"[^\d.\-]", "", raw)
    try:
        val = float(cleaned)
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
# Generic JSON bid extractor — handles any API response shape
# ---------------------------------------------------------------------------

_GRAIN_KEYWORDS = frozenset([
    "corn", "soybean", "soy", "wheat", "bean", "milo",
    "sorghum", "oat", "meal", "oil", "ddgs",
])


def _looks_like_grain(text: str) -> bool:
    return any(kw in text.lower() for kw in _GRAIN_KEYWORDS)


def _extract_bids_from_response(body: object, elevator_name: str) -> list[dict]:
    """
    Extract bid rows from any JSON response shape.
    Tries common key names; filters to grain commodities only.
    """
    if not body:
        return []

    # Flatten nested structures to get a list of candidate items
    items: list = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        for key in ("data", "cashBids", "bids", "results", "items",
                    "quotes", "markets", "rows", "records"):
            val = body.get(key)
            if isinstance(val, list):
                items = val
                break
            if isinstance(val, dict):
                for sub in ("cashBids", "bids", "data", "results"):
                    sv = val.get(sub)
                    if isinstance(sv, list):
                        items = sv
                        break
                if items:
                    break
        if not items:
            # Last resort: search all list-valued keys
            for val in body.values():
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                    items = val
                    break

    bids = []
    for item in items:
        if not isinstance(item, dict):
            continue

        commodity = str(
            item.get("commodity") or item.get("name") or item.get("commodityName")
            or item.get("description") or item.get("grainType") or ""
        ).strip()

        if not commodity or not _looks_like_grain(commodity):
            continue

        delivery = str(
            item.get("deliveryPeriod") or item.get("delivery") or item.get("period")
            or item.get("expirationDate") or item.get("contractName")
            or item.get("month") or item.get("contract") or ""
        ).strip()

        cash_raw = (
            item.get("cashPrice") or item.get("price") or item.get("bidPrice")
            or item.get("bid") or item.get("cash") or None
        )
        basis_raw = (
            item.get("basis") or item.get("basisPrice") or item.get("basisAmount") or None
        )
        futures_ref = str(
            item.get("futuresCode") or item.get("futuresSymbol") or item.get("contract")
            or item.get("contractCode") or item.get("symbol") or ""
        ).strip()
        change_raw = (
            item.get("netChange") or item.get("change") or item.get("priceChange") or None
        )

        cash_val = _parse_price(str(cash_raw)) if cash_raw is not None else None

        bids.append({
            "elevator": elevator_name,
            "commodity": commodity,
            "delivery_period": delivery,
            "cash_price": cash_val,
            "basis": _parse_basis(str(basis_raw)) if basis_raw is not None else None,
            "futures_reference": futures_ref,
            "change": _parse_price(str(change_raw)) if change_raw is not None else None,
        })

    return bids


# ---------------------------------------------------------------------------
# Barchart WebSol helper: intercept any JSON response containing bid data
# ---------------------------------------------------------------------------

async def _fetch_barchart_bids(
    page: Page,
    url: str,
    elevator_name: str,
) -> list[dict]:
    """
    For Barchart-WebSol-powered sites: capture cash-bid data from any JSON
    response.  Falls back to parsing rendered HTML on failure.

    Key changes vs. prior version:
    - wait_until="load" (not "networkidle") — prevents 30-s stall on
      sites that keep polling forever.
    - Captures ALL non-HTML/non-image JSON responses, not just URL-matched ones.
    - Reduced selector wait (3 s each, 3 selectors) to cut dead time.
    - asyncio.sleep shortened to 5 s.
    """
    captured_bids: list[dict] = []
    api_captured = False

    async def handle_response(response):
        nonlocal api_captured
        try:
            if response.status >= 400:
                return
            ct = response.headers.get("content-type", "")
            # Skip obvious non-data types
            if any(s in ct for s in (
                "text/html", "image/", "font/", "text/css",
            )):
                return
            # Accept json, empty content-type, and anything not explicitly excluded
            try:
                body = await response.json()
            except Exception:
                try:
                    text = await response.text()
                    body = json.loads(text)
                except Exception:
                    return

            bids = _extract_bids_from_response(body, elevator_name)
            if bids:
                api_captured = True
                captured_bids.extend(bids)
                logger.debug(
                    f"{elevator_name}: captured {len(bids)} bids from {response.url[:80]}"
                )
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        await page.goto(url, wait_until="load", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        logger.warning(f"{elevator_name}: navigation: {e}")

    # Wait briefly for widget/content selectors — 3 s each, stop on first hit
    for sel in ("[class*='bid']", "[class*='cash']", "table tbody tr td"):
        try:
            await page.wait_for_selector(sel, timeout=3_000)
            break
        except Exception:
            continue

    await asyncio.sleep(5)

    if api_captured and captured_bids:
        logger.info(f"{elevator_name}: {len(captured_bids)} bids via JSON intercept")
        return captured_bids

    # Fallback: parse HTML table
    logger.info(f"{elevator_name}: JSON intercept missed; falling back to HTML parse")
    return await _parse_barchart_html(page, elevator_name)


async def _parse_barchart_html(page: Page, elevator_name: str) -> list[dict]:
    """Parse rendered Barchart cash-bid table from page HTML."""
    bids = []
    try:
        selectors = [
            "table.cash-bids", "table.bids-table", ".cashbid-table table",
            "table[class*='cashbid']", "table[class*='cash-bid']",
            "table[class*='bids']", "div.cashbid", "table",
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

    Fallback method (SCOULAR_USER + SCOULAR_PASS set, no cookies):
      Attempts username/password login.  If Scoular demands MFA the run will be
      skipped — refresh SCOULAR_COOKIES instead.
    """
    elevator_name = "Scoular CBLOC"
    url = "https://scoularview.com/cbloc-1941"

    cookies_json = os.environ.get("SCOULAR_COOKIES", "")
    username = os.environ.get("SCOULAR_USER", "")
    password = os.environ.get("SCOULAR_PASS", "")

    if not cookies_json and not (username and password):
        logger.warning(f"{elevator_name}: no credentials configured; skipping")
        return [], []

    page = await _new_page(context, timeout=60_000)
    try:
        if cookies_json:
            try:
                cookies = json.loads(cookies_json)
                await context.add_cookies(cookies)
                logger.info(f"{elevator_name}: injected {len(cookies)} saved cookies")
            except (json.JSONDecodeError, Exception) as exc:
                logger.error(f"{elevator_name}: failed to parse SCOULAR_COOKIES — {exc}")
                return [], []

            await page.goto(url, wait_until="load", timeout=60_000)

            bounced = any(kw in page.url.lower() for kw in ("login", "signin", "auth", "bushel"))
            if bounced and "scoularview.com" not in page.url.lower():
                logger.error(
                    f"{elevator_name}: cookies appear expired (landed on {page.url}) — "
                    "re-run get_scoular_cookies.py locally and update SCOULAR_COOKIES"
                )
                return [], []

            logger.info(f"{elevator_name}: cookie login succeeded; on {page.url}")

        else:
            logger.info(f"{elevator_name}: no cookies; attempting username/password login")
            await page.goto(url, wait_until="load", timeout=60_000)

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
                await page.wait_for_load_state("load", timeout=60_000)

            mfa_present = await page.query_selector(
                "input[name='code'], input[name='otp'], input[name='token'], "
                "input[maxlength='6'], input[placeholder*='code' i], input[placeholder*='security' i]"
            )
            if mfa_present:
                logger.error(
                    f"{elevator_name}: MFA prompt detected — run get_scoular_cookies.py locally "
                    "and store output as SCOULAR_COOKIES"
                )
                return [], []

            if url not in page.url:
                await page.goto(url, wait_until="load", timeout=60_000)

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

        fresh_cookies: list[dict] = []
        if bids:
            try:
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
    ADM Gradable React app scraper (shared for Decatur Soy, Decatur Corn, Sauget).
    Captures any JSON API response, falls back to HTML parse.
    """
    page = await _new_page(context)
    try:
        logger.info(f"{elevator_name}: fetching {url}")

        api_bids: list[dict] = []
        api_hit = False

        async def handle_response(response):
            nonlocal api_hit
            try:
                if response.status >= 400:
                    return
                ct = response.headers.get("content-type", "")
                if any(s in ct for s in ("text/html", "image/", "font/", "text/css")):
                    return
                try:
                    body = await response.json()
                except Exception:
                    try:
                        body = json.loads(await response.text())
                    except Exception:
                        return

                bids = _extract_bids_from_response(body, elevator_name)
                if bids:
                    api_hit = True
                    api_bids.extend(bids)
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="load", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: navigation: {e}")

        for sel in ("[class*='bid']", "table tbody tr", "[class*='BidTable']"):
            try:
                await page.wait_for_selector(sel, timeout=3_000)
                break
            except Exception:
                continue

        await asyncio.sleep(5)

        if api_hit and api_bids:
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API intercept")
            return api_bids

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
        table = await page.query_selector("table")
        if table:
            rows = await table.query_selector_all("tbody tr, tr")
            for row in rows:
                cells = await row.query_selector_all("td, th")
                vals = [await c.inner_text() for c in cells]
                vals = [v.strip() for v in vals]
                if len(vals) < 3:
                    continue
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

        cards = await page.query_selector_all(
            ".bid-card, [class*='bidCard'], [class*='BidCard'], [class*='bid-row'], [class*='BidRow']"
        )
        for card in cards:
            text = await card.inner_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
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

    try:
        logger.info(f"{elevator_name}: fetching {url}")

        api_bids: list[dict] = []
        api_hit = False

        async def handle_response(response):
            nonlocal api_hit
            try:
                if response.status >= 400:
                    return
                ct = response.headers.get("content-type", "")
                if any(s in ct for s in ("text/html", "image/", "font/", "text/css")):
                    return
                try:
                    body = await response.json()
                except Exception:
                    try:
                        body = json.loads(await response.text())
                    except Exception:
                        return

                # Try generic extraction first, then filter by location
                items: list = []
                if isinstance(body, list):
                    items = body
                elif isinstance(body, dict):
                    for key in ("data", "cashBids", "bids", "results", "items"):
                        if isinstance(body.get(key), list):
                            items = body[key]
                            break

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    loc = str(
                        item.get("location") or item.get("locationName")
                        or item.get("elevator") or item.get("city") or ""
                    ).lower()
                    commodity = str(
                        item.get("commodity") or item.get("name") or ""
                    ).strip()

                    if not commodity or not _looks_like_grain(commodity):
                        continue

                    # Include if location matches or if no location field (let user filter visually)
                    if loc and not any(tl in loc for tl in target_locations):
                        continue

                    display_name = f"CHS {loc.title()}" if loc else elevator_name
                    api_hit = True
                    api_bids.append({
                        "elevator": display_name,
                        "commodity": commodity,
                        "delivery_period": str(
                            item.get("deliveryPeriod") or item.get("period") or ""
                        ),
                        "cash_price": float(item["cashPrice"]) if item.get("cashPrice") is not None else None,
                        "basis": float(item["basis"]) if item.get("basis") is not None else None,
                        "futures_reference": str(item.get("futuresCode") or ""),
                        "change": float(item["change"]) if item.get("change") is not None else None,
                    })

                # Also try generic extraction for any remaining data
                if not api_hit:
                    bids = _extract_bids_from_response(body, elevator_name)
                    if bids:
                        api_hit = True
                        api_bids.extend(bids)

            except Exception:
                pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="load", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: nav warning: {e}")

        await asyncio.sleep(5)

        if api_hit and api_bids:
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API (Lowder+Cahokia)")
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
        sections = await page.query_selector_all(
            "section, .location-section, [class*='location'], [class*='elevator'], h2, h3, h4"
        )

        current_location = None
        for elem in sections:
            text = await elem.inner_text()
            text_lower = text.lower().strip()
            if any(tl in text_lower for tl in target_locations):
                current_location = text.strip()
                table = await elem.evaluate_handle("(el) => el.nextElementSibling")
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
            bids = await _parse_barchart_html(page, elevator_name)
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

    try:
        logger.info(f"{elevator_name}: fetching {url}")

        api_bids: list[dict] = []
        api_hit = False

        async def handle_response(response):
            nonlocal api_hit
            try:
                if response.status >= 400:
                    return
                ct = response.headers.get("content-type", "")
                if any(s in ct for s in ("text/html", "image/", "font/", "text/css")):
                    return
                try:
                    body = await response.json()
                except Exception:
                    try:
                        body = json.loads(await response.text())
                    except Exception:
                        return

                items: list = []
                if isinstance(body, list):
                    items = body
                elif isinstance(body, dict):
                    for key in ("data", "bids", "results", "cashBids", "locations", "items"):
                        if isinstance(body.get(key), list):
                            items = body[key]
                            break

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    loc = str(
                        item.get("location") or item.get("city")
                        or item.get("name") or item.get("locationName") or ""
                    ).lower()
                    commodity = str(item.get("commodity") or "Corn").strip()

                    if loc and target_location not in loc:
                        continue

                    if not _looks_like_grain(commodity):
                        continue

                    api_hit = True
                    api_bids.append({
                        "elevator": elevator_name,
                        "commodity": commodity,
                        "delivery_period": str(
                            item.get("deliveryPeriod") or item.get("period")
                            or item.get("month") or ""
                        ),
                        "cash_price": float(item["cashPrice"]) if item.get("cashPrice") is not None else None,
                        "basis": float(item["basis"]) if item.get("basis") is not None else None,
                        "futures_reference": str(item.get("futuresCode") or ""),
                        "change": float(item["change"]) if item.get("change") is not None else None,
                    })

                if not api_hit:
                    bids = _extract_bids_from_response(body, elevator_name)
                    if bids:
                        api_hit = True
                        api_bids.extend(bids)

            except Exception:
                pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="load", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{elevator_name}: nav warning: {e}")

        await asyncio.sleep(5)

        if api_hit and api_bids:
            logger.info(f"{elevator_name}: {len(api_bids)} bids via API (Madison IL)")
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
        all_text = await page.evaluate("document.body.innerText")
        if target_location.lower() not in all_text.lower():
            logger.warning(f"{elevator_name}: '{target_location}' not found on page")
            return bids

        tables = await page.query_selector_all("table")
        for table in tables:
            parent_text = await table.evaluate(
                "(el) => { let p = el.parentElement; return p ? p.innerText : ''; }"
            )
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
            bids = await _parse_barchart_html(page, elevator_name)
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
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-popup-blocking",
                "--no-first-run",
                "--no-default-browser-check",
                "--ignore-certificate-errors",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1920,1080",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/Chicago",
            java_script_enabled=True,
            has_touch=False,
            is_mobile=False,
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
        )
        # Apply stealth — must be set on context before pages are created
        await context.add_init_script(_STEALTH_SCRIPT)

        # Block images and fonts to speed up page loads
        await context.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,webp,woff,woff2,ttf,eot}",
            lambda route: route.abort(),
        )

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

    output: dict[str, list[dict]] = {}
    scoular_fresh_cookies: list[dict] = []
    if isinstance(scoular_result, Exception):
        logger.error(f"Scoular CBLOC: unhandled exception: {scoular_result}")
        output["Scoular CBLOC"] = []
    else:
        scoular_bids, scoular_fresh_cookies = scoular_result
        output["Scoular CBLOC"] = scoular_bids

    for name, result in zip(non_scoular_tasks.keys(), other_results):
        if isinstance(result, Exception):
            logger.error(f"{name}: unhandled exception: {result}")
            output[name] = []
        else:
            output[name] = result

    total_bids = sum(len(v) for v in output.values())
    logger.info(f"Elevator scraping complete: {total_bids} total bids from {len(output)} elevators")
    return output, scoular_fresh_cookies
