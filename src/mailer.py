"""
mailer.py - Gmail SMTP sender for the Daily Grain Brief.

Uses smtplib with SSL on port 465 (smtp.gmail.com).
Credentials loaded from environment variables:
  GMAIL_FROM       - sender address (e.g., wesseifert1995@gmail.com)
  GMAIL_APP_PASSWORD - Gmail app password (not the regular account password)
  EMAIL_TO         - recipient address (e.g., wes@seifert.farm)
"""

import logging
import os
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

# Load .env for local runs (GitHub Actions uses secrets instead)
load_dotenv()

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def build_subject(report_date: date | None = None) -> str:
    """Build the email subject line."""
    if report_date is None:
        report_date = date.today()
    weekday = report_date.strftime("%A")
    date_str = report_date.strftime("%B %d, %Y")
    return f"🌽 Daily Grain Brief | {weekday}, {date_str}"


def send_email(html_body: str, report_date: date | None = None) -> None:
    """
    Send the HTML email via Gmail SMTP.

    Args:
        html_body: Full HTML content of the email.
        report_date: Date to use in the subject (defaults to today).

    Raises:
        ValueError: If required environment variables are not set.
        smtplib.SMTPException: If the SMTP connection or send fails.
    """
    gmail_from = os.environ.get("GMAIL_FROM", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    email_to = os.environ.get("EMAIL_TO", "").strip()

    # Validate required env vars
    missing = []
    if not gmail_from:
        missing.append("GMAIL_FROM")
    if not app_password:
        missing.append("GMAIL_APP_PASSWORD")
    if not email_to:
        missing.append("EMAIL_TO")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Set them in .env (local) or GitHub Secrets (CI)."
        )

    subject = build_subject(report_date)

    # Build MIME message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_from
    msg["To"] = email_to

    # Plain-text fallback
    plain_text = (
        "Your email client does not support HTML. "
        "Please view this email in an HTML-capable client.\n\n"
        f"Daily Grain Brief — {subject}"
    )
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info(f"Sending email: '{subject}' from {gmail_from} to {email_to}")

    # Connect via SSL and send
    ssl_context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl_context) as server:
            server.login(gmail_from, app_password)
            server.sendmail(gmail_from, [email_to], msg.as_bytes())
        logger.info(f"Email sent successfully to {email_to}")
    except smtplib.SMTPAuthenticationError as e:
        logger.error(
            f"Gmail SMTP authentication failed. "
            f"Ensure GMAIL_APP_PASSWORD is a valid App Password (not your account password). "
            f"Error: {e}"
        )
        raise
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error while sending email: {e}")
        raise
    except OSError as e:
        logger.error(f"Network error connecting to {SMTP_HOST}:{SMTP_PORT}: {e}")
        raise
