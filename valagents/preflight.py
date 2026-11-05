"""Go-live checklist: shows what is set, what's still a placeholder, and what's switched off. Prints no secret values."""
from __future__ import annotations

import json
import sys

from .settings import load


def report(s, cfg: dict) -> list[tuple[str, str, str]]:
    items = [
        ("Core secrets", "SET" if not s.validate_for_production() else "MISSING: " + ", ".join(s.validate_for_production()), "Blayke"),
        ("Public app URL", "SET" if "localhost" not in s.public_url else "PLACEHOLDER: PUBLIC_URL", "Blayke"),
        ("Val's rates", "CONFIRMED" if cfg.get("rates_confirmed") else "PLACEHOLDER: rates_confirmed false", "Val"),
        ("Val's iCal link", "SET" if s.ics_url else "PLACEHOLDER: VAL_ICS_URL", "Val"),
        ("Booking email address", "SET" if s.booking_email else "PLACEHOLDER: VAL_BOOKING_EMAIL", "Val"),
        ("Inbox app password", "SET" if s.imap_user and s.imap_password else "PLACEHOLDER: IMAP_USER, IMAP_APP_PASSWORD", "Val"),
        ("Email start date", "SET" if s.email_start_date else "PLACEHOLDER: EMAIL_START_DATE", "Blayke"),
        ("Twilio number", "SET" if s.twilio_number else "PLACEHOLDER: TWILIO_NUMBER", "Blayke"),
        ("Twilio auth token", "SET" if s.twilio_auth_token else "PLACEHOLDER: TWILIO_AUTH_TOKEN", "Blayke"),
        ("ElevenLabs voice", "SET" if s.elevenlabs_voice else "PLACEHOLDER: set up in ElevenLabs", "Blayke"),
        ("Where Val's alerts go", "SET" if s.alert_val else "PLACEHOLDER: ALERT_VAL", "Val"),
        ("Gmail for sending alerts", "SET" if s.smtp_user and s.smtp_password else "PLACEHOLDER: SMTP_USER, SMTP_APP_PASSWORD", "Blayke"),
        ("Gig tracker sheet", "SET" if s.gig_sheet_id and s.google_sa_json else "PLACEHOLDER: GIG_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON", "Blayke"),
        ("Clash check", "ON" if s.clash_on(cfg) else "OFF", "Blayke turns on"),
        ("Quote approval", "ON" if s.quote_on(cfg) else "OFF", "Blayke turns on"),
        ("Email intake", "ON" if s.email_intake_on() else "OFF", "Blayke turns on"),
        ("Dry run (nothing really sends)", "ON" if s.dry_run else "OFF", "Blayke turns off"),
    ]
    return items


def main() -> int:
    s = load()
    cfg = json.loads(open(s.config_path).read())
    for name, state, owner in report(s, cfg):
        print(f"{name:34} {state:62} {owner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
