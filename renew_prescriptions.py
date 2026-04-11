"""
RX365 Prescription Auto-Renewal
Logs into https://citycenterpharmacy1175.rx365.com, renews prescriptions for
the primary user and the wife's account, then sends a notification.

Credentials and Rx# lists are read from environment variables (GitHub Secrets).
"""

import os
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

def login(page: Page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="networkidle")
    # Fill credentials — try label-based selectors first, then placeholder fallbacks
    username_field = page.get_by_label("Username")
    if username_field.count() == 0:
        username_field = page.get_by_placeholder("Username")
    username_field.fill(username)

    password_field = page.get_by_label("Password")
    if password_field.count() == 0:
        password_field = page.get_by_placeholder("Password")
    password_field.fill(password)

    page.get_by_role("button", name="Log In").click()
    # Wait until the post-login "Menu" element appears
    page.wait_for_selector("text=Menu", timeout=20_000)
    print("  Login successful.")


def logout(page: Page) -> None:
    page.get_by_text("Menu").click()
    # Try common logout label variants
    for label in ["Log Out", "Logout", "Sign Out", "Sign out"]:
        locator = page.get_by_text(label, exact=True)
        if locator.count() > 0:
            locator.click()
            break
    page.wait_for_selector("text=Log In", timeout=10_000)
    print("  Logged out.")


def perform_refill(page: Page, rx_numbers: list[str]) -> None:
    pickup_date = calculate_pickup_date()
    formatted_date = pickup_date.strftime(DATE_FORMAT)

    # ── Navigate to Refill Multiple ───────────────────────────────────────────
    page.get_by_text("Menu").click()
    page.get_by_text("Medications").click()
    page.wait_for_selector("text=Refill Multiple", timeout=10_000)
    page.get_by_text("Refill Multiple").click()

    # ── Select prescriptions by Rx# ───────────────────────────────────────────
    # Identify each prescription by its unique Rx# to avoid duplicate-name issues.
    for rx_num in rx_numbers:
        # Find the text "Rx# <number>", then walk up to its containing card/row
        rx_text = page.locator(f"text=Rx# {rx_num}")
        rx_text.wait_for(timeout=10_000)
        # Try multiple ancestor tag patterns to find the card boundary
        row = None
        for xpath in [
            "xpath=ancestor::li[1]",
            "xpath=ancestor::div[contains(@class,'card')][1]",
            "xpath=ancestor::div[contains(@class,'item')][1]",
            "xpath=ancestor::div[contains(@class,'row')][1]",
            "xpath=ancestor::section[1]",
        ]:
            candidate = rx_text.locator(xpath)
            if candidate.count() > 0:
                row = candidate
                break

        if row is None:
            # Last resort: check the nearest checkbox anywhere on the page
            # that is visually closest to this Rx# text
            print(f"  WARNING: Could not find container for Rx# {rx_num}, trying page-level checkbox")
            page.locator("input[type='checkbox']").nth(rx_numbers.index(rx_num)).check()
        else:
            checkbox = row.get_by_role("checkbox")
            if checkbox.count() == 0:
                checkbox = row.locator("input[type='checkbox']")
            checkbox.check()

        print(f"  Checked Rx# {rx_num}")

    # ── Open the date/time dialog ─────────────────────────────────────────────
    page.get_by_text("Request Refill").first.click()

    # ── Date selection ────────────────────────────────────────────────────────
    page.get_by_text("Select a date").click()

    # Try text input (type="text" or type="date")
    date_input = page.get_by_label("Date")
    if date_input.count() > 0 and date_input.first.is_visible():
        date_input.first.fill(formatted_date)
    else:
        # Calendar grid fallback: click the correct day cell
        day_str = str(pickup_date.day)
        page.get_by_role("gridcell", name=day_str, exact=True).click()

    # ── Time selection ────────────────────────────────────────────────────────
    # The time picker is a grid of buttons (10:30 AM, 11:00 AM, 11:30 AM …)
    # inside a "Confirm Refill Request" modal. Click the time then "Select".
    page.get_by_text("Select a time").click()
    page.get_by_role("button", name=PICKUP_TIME, exact=True).click()
    page.get_by_role("button", name="Select").click()

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
