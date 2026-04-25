"""
RX365 Prescription Auto-Renewal
Logs into https://citycenterpharmacy1175.rx365.com, renews prescriptions for
the primary user and the wife's account, then sends a notification.

Credentials and Rx# lists are read from environment variables (GitHub Secrets).
"""

import os
import re
import sys
import smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import (
    Browser,
    Page,
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

# ── Constants ──────────────────────────────────────────────────────────────────
LOGIN_URL = "https://citycenterpharmacy1175.rx365.com/login?mismatched=true"
DATE_FORMAT = "%m/%d/%Y"  # Adjust if RX365 expects a different format (e.g. "%Y-%m-%d")
PICKUP_TIME = "11:00 AM"
NOTIFICATION_EMAIL = "deadtreeproducts01@gmail.com"
SMS_PHONE = "8015898781"

# ── Secrets (fail fast if any required secret is missing) ─────────────────────
RX365_USER_USERNAME = os.environ["RX365_USER_USERNAME"]
RX365_USER_PASSWORD = os.environ["RX365_USER_PASSWORD"]
RX365_USER_RX_NUMBERS = [
    n.strip() for n in os.environ["RX365_USER_RX_NUMBERS"].split(",") if n.strip()
]
RX365_WIFE_USERNAME = os.environ["RX365_WIFE_USERNAME"]
RX365_WIFE_PASSWORD = os.environ["RX365_WIFE_PASSWORD"]
RX365_WIFE_RX_NUMBER = os.environ["RX365_WIFE_RX_NUMBER"].strip()

# Optional — notifications won't be sent if blank
NOTIFICATION_EMAIL_PASSWORD = os.environ.get("NOTIFICATION_EMAIL_PASSWORD", "")
# e.g. "txt.att.net" for AT&T, "tmomail.net" for T-Mobile, "vtext.com" for Verizon
CARRIER_SMS_GATEWAY = os.environ.get("CARRIER_SMS_GATEWAY", "")


# ── Helpers ────────────────────────────────────────────────────────────────────

def calculate_pickup_date() -> date:
    """Return today unless today is Sunday, in which case return Monday."""
    today = date.today()
    if today.weekday() == 6:  # 6 = Sunday
        return today + timedelta(days=1)
    return today


def send_notification(subject: str, body: str) -> None:
    """Send an email notification (and optionally an SMS via carrier gateway)."""
    if not NOTIFICATION_EMAIL_PASSWORD:
        print(f"[NOTIFY] No email password set — skipping notification: {subject}")
        return

    recipients = [NOTIFICATION_EMAIL]
    if CARRIER_SMS_GATEWAY:
        recipients.append(f"{SMS_PHONE}@{CARRIER_SMS_GATEWAY}")

    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = NOTIFICATION_EMAIL
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(NOTIFICATION_EMAIL, NOTIFICATION_EMAIL_PASSWORD)
            smtp.sendmail(NOTIFICATION_EMAIL, recipients, msg.as_string())

        print(f"[NOTIFY] Sent: {subject}")
    except Exception as exc:
        print(f"[NOTIFY] Failed to send notification: {exc}", file=sys.stderr)


# ── Core automation ────────────────────────────────────────────────────────────

def _find_field(page: Page, labels: list[str], placeholders: list[str], selectors: list[str]) -> "Locator":
    """Try multiple strategies to locate a form field, return the first match."""
    for text in labels:
        loc = page.get_by_label(text, exact=False)
        if loc.count() > 0:
            return loc.first
    for text in placeholders:
        loc = page.get_by_placeholder(text, exact=False)
        if loc.count() > 0:
            return loc.first
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc.first
    # Return the last selector as a fallback (will timeout with a clear error)
    return page.locator(selectors[-1])


def login(page: Page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="networkidle")
    # Screenshot the login page so we can inspect it if anything goes wrong
    page.screenshot(path="login_page.png")

    # Dismiss the "Redirected" modal that appears on first load
    ok_btn = page.get_by_role("button", name="OK")
    if ok_btn.count() > 0:
        ok_btn.click()
        page.wait_for_timeout(500)  # brief pause for modal to close

    # Username field — try every common label/placeholder/selector pattern
    username_field = _find_field(
        page,
        labels=["Username", "User Name", "Email", "Email Address", "User ID", "Phone"],
        placeholders=["Username", "User Name", "Email", "Email Address", "User ID", "Phone Number"],
        selectors=[
            "input[name='username']", "input[id='username']",
            "input[type='email']", "input[type='tel']",
            "input[name='email']", "input[id='email']",
            "input[type='text']:visible",
        ],
    )
    username_field.fill(username)

    # Password field
    password_field = _find_field(
        page,
        labels=["Password", "Pass"],
        placeholders=["Password", "Enter password"],
        selectors=[
            "input[type='password']",
            "input[name='password']", "input[id='password']",
        ],
    )
    password_field.fill(password)

    # Login button — try common button text variants
    for btn_name in ["Log In", "Login", "Sign In", "Submit"]:
        btn = page.get_by_role("button", name=btn_name, exact=False)
        if btn.count() > 0:
            btn.first.click()
            break

    # Wait until the post-login sidebar appears (desktop view shows sidebar, not hamburger)
    page.wait_for_selector("text=Medications", timeout=20_000)
    print("  Login successful.")


def logout(page: Page) -> None:
    # Desktop view has a "Sign Out" link directly in the header (no Menu click needed)
    for label in ["Sign Out", "Sign out", "Log Out", "Logout"]:
        locator = page.get_by_text(label, exact=True)
        if locator.count() > 0:
            locator.click()
            break
    page.wait_for_selector("text=Login", timeout=10_000)
    print("  Logged out.")


def perform_refill(page: Page, rx_numbers: list[str]) -> None:
    pickup_date = calculate_pickup_date()
    formatted_date = pickup_date.strftime(DATE_FORMAT)

    # ── Navigate to Refill Multiple ───────────────────────────────────────────
    page.get_by_text("Medications").click()
    page.wait_for_selector("text=Refill Multiple", timeout=10_000)
    page.get_by_text("Refill Multiple").click()
    page.wait_for_timeout(1000)

    # ── Select each prescription using the search box ─────────────────────────
    # Use the search box ("Search by medication, prescriber, Rx number") to
    # filter to each Rx# one at a time — avoids time-filter "Custom" breakage.
    search_box = page.get_by_placeholder("Search by medication, prescriber, Rx number")

    for rx_num in rx_numbers:
        # Filter the list to this Rx#
        if search_box.count() > 0:
            search_box.fill("")
            search_box.fill(rx_num)
            page.wait_for_timeout(800)

        # Find the Rx# text on the page
        rx_text = page.locator(f"text=Rx# {rx_num}")
        if rx_text.count() == 0:
            print(f"  WARNING: Rx# {rx_num} not found — may not be eligible for refill, skipping.")
            continue

        # Walk up to the card/row boundary
        row = None
        for xpath in [
            "xpath=ancestor::li[1]",
            "xpath=ancestor::div[contains(@class,'card')][1]",
            "xpath=ancestor::div[contains(@class,'item')][1]",
            "xpath=ancestor::div[contains(@class,'row')][1]",
            "xpath=ancestor::section[1]",
            "xpath=ancestor::div[3]",
        ]:
            candidate = rx_text.locator(xpath)
            if candidate.count() > 0:
                row = candidate
                break

        if row is None:
            print(f"  WARNING: Could not locate card for Rx# {rx_num}, skipping.")
            continue

        # The visible circle is a custom-styled div; the actual <input> is hidden/readonly.
        # Strategy 1: click a div with RadioButton in its class (outer circle container)
        # Strategy 2: click the entire card row (may have an onClick handler)
        # Strategy 3: dispatch a native JS click on the hidden input (React will handle it)
        clicked = False
        radio_div = row.locator("div[class*='RadioButton']").first
        if radio_div.count() > 0:
            try:
                radio_div.click(timeout=3000)
                clicked = True
            except Exception:
                pass

        if not clicked:
            try:
                row.click(timeout=3000)
                clicked = True
            except Exception:
                pass

        if not clicked:
            hidden_input = row.locator("input[type='checkbox']").first
            page.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}))",
                          hidden_input.element_handle())

        print(f"  Selected Rx# {rx_num}")

    # Clear the search so the full selection is visible before submitting
    if search_box.count() > 0:
        search_box.fill("")
        page.wait_for_timeout(500)

    # ── Open the date/time dialog ─────────────────────────────────────────────
    page.get_by_text("Request Refill").first.click()

    # ── Date selection (calendar grid picker) ─────────────────────────────────
    page.get_by_text("Select a date").click()
    page.wait_for_timeout(1200)  # wait for calendar to render
    page.screenshot(path="calendar_open.png")

    day_str = str(pickup_date.day)
    iso_date = pickup_date.strftime("%Y-%m-%d")

    # Dump the calendar container HTML so we can see the exact element structure
    cal_html = page.evaluate(r"""() => {
        const selectors = [
            '[class*="Calendar"]', '[class*="calendar"]',
            '[class*="DatePicker"]', '[class*="datePicker"]',
            '[class*="Picker"]', '[class*="picker"]',
            '[class*="Modal"]', '[class*="modal"]',
            '[class*="Dialog"]', '[class*="dialog"]',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) return sel + ': ' + el.innerHTML.slice(0, 3000);
        }
        return 'no calendar container found — body: ' + document.body.innerHTML.slice(0, 3000);
    }""")
    print(f"  Calendar HTML: {cal_html}")

    clicked_day = False

    # Strategy 1: any element (div/span/p/li/button) whose complete text is the day number
    js_result = page.evaluate(f"""() => {{
        const allEls = Array.from(document.querySelectorAll('*'));
        // Find leaf-like elements whose trimmed text is exactly our day number
        const candidates = allEls.filter(el => {{
            const txt = el.textContent.trim();
            if (txt !== '{day_str}') return false;
            // Prefer elements with no element children (true leaf) or one child
            return el.children.length <= 1;
        }});
        // Log what we found before clicking
        const found = candidates.map(el => ({{
            tag: el.tagName,
            cls: el.className.slice(0, 80),
            aria: el.getAttribute('aria-label'),
            children: el.children.length,
            outerHTML: el.outerHTML.slice(0, 150),
        }}));
        if (candidates.length === 0) {{
            // Widen net: any element containing the day number as its sole text node
            const wider = allEls.filter(el => {{
                const nodes = Array.from(el.childNodes);
                const textOnly = nodes.filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
                return textOnly === '{day_str}';
            }});
            return 'none-exact. wider=' + JSON.stringify(wider.slice(0,5).map(el => ({{
                tag: el.tagName, cls: el.className.slice(0,80), outer: el.outerHTML.slice(0,150)
            }})));
        }}
        // Click the first non-disabled candidate
        for (const el of candidates) {{
            const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true'
                          || el.classList.contains('disabled') || el.classList.contains('past');
            if (!disabled) {{
                el.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true, cancelable:true}}));
                el.dispatchEvent(new MouseEvent('mouseup',   {{bubbles:true, cancelable:true}}));
                el.click();
                return 'clicked: ' + el.tagName + ' cls=' + el.className + ' | ' + el.outerHTML.slice(0, 200);
            }}
        }}
        return 'all-disabled: ' + JSON.stringify(found);
    }}""")
    print(f"  Day click JS result: {js_result}")
    if js_result and js_result.startswith("clicked:"):
        page.wait_for_timeout(400)
        clicked_day = True

    if not clicked_day:
        # Strategy 2: Playwright locator on div/span with exact text
        for loc in [
            page.locator("div").filter(has_text=re.compile(rf"^\s*{re.escape(day_str)}\s*$")),
            page.locator("span").filter(has_text=re.compile(rf"^\s*{re.escape(day_str)}\s*$")),
            page.locator("p").filter(has_text=re.compile(rf"^\s*{re.escape(day_str)}\s*$")),
            page.locator("li").filter(has_text=re.compile(rf"^\s*{re.escape(day_str)}\s*$")),
        ]:
            try:
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(400)
                    clicked_day = True
                    print(f"  Clicked day via Playwright div/span locator")
                    break
            except Exception as e:
                print(f"  div/span locator attempt failed: {e}")

    if not clicked_day:
        print(f"  WARNING: Could not click day {day_str} — check 'Calendar HTML' log above for element structure")

    page.screenshot(path="calendar_after_click.png")

    # Confirm the date selection with the "Select" button at the bottom
    page.get_by_role("button", name="Select").click()
    page.wait_for_timeout(500)

    # ── Time selection ────────────────────────────────────────────────────────
    page.get_by_text("Select a time").click()
    page.wait_for_timeout(500)
    page.get_by_role("button", name=PICKUP_TIME, exact=True).click()
    page.get_by_role("button", name="Select").click()
    page.wait_for_timeout(500)

    # ── Final confirmation ────────────────────────────────────────────────────
    page.get_by_text("Request Refill").last.click()

    # Wait for a success indicator (adjust text if the site uses different wording)
    page.wait_for_selector(
        "text=/successfully|confirmed|submitted/i", timeout=20_000
    )
    print(f"  Refill requested. Pickup: {formatted_date} at {PICKUP_TIME}")


def run_user(
    browser: Browser,
    label: str,
    username: str,
    password: str,
    rx_numbers: list[str],
) -> None:
    """Run the full refill flow for one account in an isolated browser context."""
    context = browser.new_context()
    page = context.new_page()
    screenshot_path = f"error_{label.lower().replace(' ', '_')}.png"
    try:
        print(f"\n=== {label} ===")
        login(page, username, password)
        perform_refill(page, rx_numbers)
        logout(page)

        pickup_date = calculate_pickup_date()
        send_notification(
            subject=f"RX365 Refill Complete — {label}",
            body=(
                f"Prescriptions successfully renewed for {label}.\n"
                f"Rx numbers: {', '.join(rx_numbers)}\n"
                f"Pickup: {pickup_date.strftime(DATE_FORMAT)} at {PICKUP_TIME}"
            ),
        )
    except (PlaywrightTimeoutError, Exception) as exc:
        page.screenshot(path=screenshot_path)
        send_notification(
            subject=f"RX365 Refill FAILED — {label}",
            body=(
                f"Automation failed for {label}.\n"
                f"Error: {exc}\n"
                f"A screenshot has been saved as a workflow artifact ({screenshot_path})."
            ),
        )
        raise
    finally:
        context.close()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run_user(
                browser,
                label="Primary User",
                username=RX365_USER_USERNAME,
                password=RX365_USER_PASSWORD,
                rx_numbers=RX365_USER_RX_NUMBERS,
            )
            run_user(
                browser,
                label="Wife",
                username=RX365_WIFE_USERNAME,
                password=RX365_WIFE_PASSWORD,
                rx_numbers=[RX365_WIFE_RX_NUMBER],
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
