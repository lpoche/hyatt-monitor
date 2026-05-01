#!/usr/bin/env python3
"""
Daily monitor for Hyatt House New Orleans Downtown (MSYXH) Mardi Gras 2027 rates.
Scrapes the Hyatt booking page, diffs against yesterday's snapshot, and emails
on any change. Designed to run as a GitHub Actions scheduled job.
"""

import asyncio
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROPERTY_CODE = "msyxh"
CHECK_IN = "2027-02-04"
CHECK_OUT = "2027-02-09"
ADULTS = 2

HYATT_URL = (
    f"https://www.hyatt.com/shop/rooms/{PROPERTY_CODE}"
    f"?checkinDate={CHECK_IN}&checkoutDate={CHECK_OUT}&numberOfAdults={ADULTS}"
)

STORAGE_FILE = "last_known_rates.json"
SCREENSHOT_FILE = "debug_screenshot.png"

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
# Comma-separated: "you@gmail.com,dad@example.com"
EMAIL_RECIPIENTS = [r.strip() for r in os.environ["EMAIL_RECIPIENTS"].split(",")]


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def scrape_rates() -> dict | None:
    """
    Launch a non-headless Chromium (via Xvfb in CI) to load the Hyatt booking
    page. Intercepts XHR/fetch responses to capture raw rate JSON, then falls
    back to DOM text extraction if no API call is caught.

    Returns a dict with keys: source, data, timestamp.
    Returns None if the page appears blocked or empty.
    """
    captured: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/Chicago",
        )

        # Mask the automation flag that Akamai checks
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        page = await context.new_page()

        # Capture any XHR/fetch response that returns JSON
        async def on_response(response):
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                data = await response.json()
                captured.append({"url": response.url, "data": data})
            except Exception:
                pass

        page.on("response", on_response)

        # Visit the homepage first to pick up session cookies
        print("  Loading hyatt.com homepage...")
        await page.goto("https://www.hyatt.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Navigate to the booking page
        print(f"  Loading booking page: {HYATT_URL}")
        await page.goto(HYATT_URL, wait_until="networkidle", timeout=90000)
        await asyncio.sleep(5)  # let lazy-loaded JS finish

        # Always capture a screenshot for debugging (uploaded as GH Actions artifact)
        await page.screenshot(path=SCREENSHOT_FILE, full_page=True)
        print(f"  Screenshot saved to {SCREENSHOT_FILE}")

        # --- Attempt 1: XHR/fetch interception ---
        api_result = _parse_api_captures(captured)
        if api_result:
            await browser.close()
            return {
                "source": "api",
                "endpoint": api_result["url"],
                "data": api_result["data"],
                "timestamp": datetime.utcnow().isoformat(),
            }

        # --- Attempt 2: DOM text extraction ---
        print("  No API JSON captured — falling back to DOM extraction.")
        dom_result = await _extract_dom(page)
        await browser.close()

        if dom_result:
            return {
                "source": "dom",
                "data": dom_result,
                "timestamp": datetime.utcnow().isoformat(),
            }

        print("  WARNING: Page appears blocked or empty (Akamai may have triggered).")
        return None


def _parse_api_captures(captured: list[dict]) -> dict | None:
    """Return the first captured response that looks like room/rate data."""
    rate_keywords = {"rate", "room", "avail", "price", "amount", "offer", "shop"}
    for item in captured:
        url = item["url"].lower()
        if not any(kw in url for kw in rate_keywords):
            continue
        data_str = json.dumps(item["data"]).lower()
        if any(kw in data_str for kw in ("rateamount", "roomtype", "minimumstay", "bestrate", "nightly")):
            return item
    # Fall back: return any JSON from the shop subdomain
    for item in captured:
        if "shop" in item["url"].lower() and isinstance(item["data"], dict):
            return item
    return None


async def _extract_dom(page) -> list[str] | None:
    """Last-resort: grab visible text from room-card-like elements."""
    selectors = [
        "[data-testid*='room']",
        "[class*='RoomCard']",
        "[class*='room-card']",
        "[class*='room-type']",
        "[class*='rate-card']",
        "[class*='RateCard']",
        "article",  # many SPAs wrap cards in <article>
    ]
    for selector in selectors:
        try:
            els = await page.query_selector_all(selector)
            if els:
                texts = [await el.inner_text() for el in els]
                texts = [t.strip() for t in texts if t.strip()]
                if texts:
                    print(f"  DOM extraction succeeded with selector: {selector!r}")
                    return texts
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def _deep_diff(old, new, path: str = "") -> list[str]:
    """Recursively diff two JSON-serialisable values. Returns human-readable lines."""
    changes = []
    if type(old) is not type(new):
        changes.append(f"{path}: type changed ({type(old).__name__} → {type(new).__name__})")
        return changes

    if isinstance(old, dict):
        all_keys = set(old) | set(new)
        for key in sorted(all_keys):
            child = f"{path}.{key}" if path else key
            if key not in old:
                changes.append(f"ADDED   {child}: {new[key]!r}")
            elif key not in new:
                changes.append(f"REMOVED {child}: {old[key]!r}")
            else:
                changes.extend(_deep_diff(old[key], new[key], child))
    elif isinstance(old, list):
        if len(old) != len(new):
            changes.append(f"{path}: list length {len(old)} → {len(new)}")
        for i, (o, n) in enumerate(zip(old, new)):
            changes.extend(_deep_diff(o, n, f"{path}[{i}]"))
    else:
        if old != new:
            changes.append(f"CHANGED {path}: {old!r} → {new!r}")
    return changes


def detect_changes(stored: dict, current: dict) -> list[str]:
    """Return a list of human-readable change descriptions, or [] if nothing changed."""
    if not stored:
        return []  # First run — no comparison yet

    old_data = stored.get("data")
    new_data = current.get("data")

    if old_data == new_data:
        return []

    # Try to produce a detailed diff
    if type(old_data) is type(new_data):
        diffs = _deep_diff(old_data, new_data)
        if diffs:
            return diffs

    # Fallback: data changed but structure shifted too much to diff cleanly
    return ["Rate data has changed (structure too different to enumerate individual fields)."]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load_stored_rates() -> dict:
    if os.path.exists(STORAGE_FILE):
        with open(STORAGE_FILE) as f:
            return json.load(f)
    return {}


def save_rates(rates: dict) -> None:
    with open(STORAGE_FILE, "w") as f:
        json.dump(rates, f, indent=2)
    print(f"  Rates saved to {STORAGE_FILE}")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(EMAIL_RECIPIENTS)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, EMAIL_RECIPIENTS, msg.as_string())
    print(f"  Email sent to: {', '.join(EMAIL_RECIPIENTS)}")


def build_change_email(changes: list[str]) -> tuple[str, str]:
    subject = (
        f"Hyatt House NOLA Mardi Gras — Change Detected {datetime.now().strftime('%b %d, %Y')}"
    )
    body = (
        "A change was detected on the Hyatt House New Orleans Downtown booking page.\n\n"
        f"Property : Hyatt House New Orleans Downtown\n"
        f"Check-in : {CHECK_IN}\n"
        f"Check-out: {CHECK_OUT}\n\n"
        "Changes detected:\n"
        + "\n".join(f"  • {c}" for c in changes)
        + f"\n\nBook here: {HYATT_URL}\n\n"
        "— Your Hyatt Monitor Bot"
    )
    return subject, body


def build_error_email() -> tuple[str, str]:
    subject = f"Hyatt House NOLA Monitor — Scrape Failed {datetime.now().strftime('%b %d, %Y')}"
    body = (
        "Today's rate check could not retrieve data from Hyatt's website.\n\n"
        "This usually means Hyatt's bot detection blocked the automated browser.\n"
        "Check the GitHub Actions run for the debug screenshot.\n\n"
        f"Booking URL: {HYATT_URL}\n\n"
        "— Your Hyatt Monitor Bot"
    )
    return subject, body


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    now = datetime.utcnow().isoformat()
    print(f"[{now}] Hyatt rate monitor starting...")
    print(f"  Target: {HYATT_URL}")

    current = await scrape_rates()

    if current is None:
        print("Scrape failed. Sending failure email.")
        subject, body = build_error_email()
        send_email(subject, body)
        sys.exit(1)

    print(f"  Data source: {current['source']}")

    stored = load_stored_rates()
    is_first_run = not stored

    changes = detect_changes(stored, current)

    if is_first_run:
        print("  First run — baseline stored. No email sent (nothing to compare).")
    elif changes:
        print(f"  {len(changes)} change(s) detected. Sending email.")
        subject, body = build_change_email(changes)
        send_email(subject, body)
    else:
        print("  No changes detected. No email sent.")

    save_rates(current)
    print(f"[{datetime.utcnow().isoformat()}] Done.")


if __name__ == "__main__":
    asyncio.run(main())
