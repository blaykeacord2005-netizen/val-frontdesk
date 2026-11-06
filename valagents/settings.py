"""All configuration. Secrets only ever come from environment variables."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Indiana/Indianapolis")


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(ROOT / "val.db")))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/"))
    dry_run: bool = field(default_factory=lambda: _flag("DRY_RUN", "1"))
    voice_secret: str = field(default_factory=lambda: os.getenv("VOICE_TOOL_SECRET", ""))
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))
    cron_secret: str = field(default_factory=lambda: os.getenv("CRON_SECRET", ""))
    ics_url: str = field(default_factory=lambda: os.getenv("VAL_ICS_URL", ""))
    elevenlabs_key: str = field(default_factory=lambda: os.getenv("ELEVENLABS_API_KEY", ""))
    elevenlabs_voice: str = field(default_factory=lambda: os.getenv("ELEVENLABS_VOICE_ID", ""))
    anthropic_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"))
    checkcherry_key: str = field(default_factory=lambda: os.getenv("CHECKCHERRY_API_KEY", ""))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_APP_PASSWORD", ""))
    alert_val: str = field(default_factory=lambda: os.getenv("ALERT_VAL", ""))
    alert_blayke: str = field(default_factory=lambda: os.getenv("ALERT_BLAYKE", ""))
    alert_max: str = field(default_factory=lambda: os.getenv("ALERT_MAX", ""))
    alert_darrell: str = field(default_factory=lambda: os.getenv("ALERT_DARRELL", ""))
    booking_email: str = field(default_factory=lambda: os.getenv("VAL_BOOKING_EMAIL", ""))
    twilio_number: str = field(default_factory=lambda: os.getenv("TWILIO_NUMBER", ""))
    twilio_auth_token: str = field(default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", ""))
    twilio_account_sid: str = field(default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", ""))
    twilio_api_base: str = field(default_factory=lambda: os.getenv("TWILIO_API_BASE", "https://api.twilio.com"))
    val_mobile: str = field(default_factory=lambda: os.getenv("VAL_MOBILE", ""))
    val_sms_ok: bool = field(default_factory=lambda: _flag("VAL_SMS_OK", "0"))
    sms_alerts_enabled: bool = field(default_factory=lambda: _flag("SMS_ALERTS_ENABLED", "0"))
    imap_sent_folder: str = field(default_factory=lambda: os.getenv("IMAP_SENT_FOLDER", "[Gmail]/Sent Mail"))
    email_intake_enabled: bool = field(default_factory=lambda: _flag("EMAIL_INTAKE_ENABLED", "0"))
    imap_host: str = field(default_factory=lambda: os.getenv("IMAP_HOST", "imap.gmail.com"))
    imap_port: int = field(default_factory=lambda: int(os.getenv("IMAP_PORT", "993")))
    imap_user: str = field(default_factory=lambda: os.getenv("IMAP_USER", ""))
    imap_password: str = field(default_factory=lambda: os.getenv("IMAP_APP_PASSWORD", ""))
    email_start_date: str = field(default_factory=lambda: os.getenv("EMAIL_START_DATE", ""))
    gig_sheet_id: str = field(default_factory=lambda: os.getenv("GIG_SHEET_ID", ""))
    google_sa_json: str = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    config_path: str = field(default_factory=lambda: os.getenv("VAL_CONFIG", str(ROOT / "data" / "val_config.json")))

    def validate_for_production(self) -> list[str]:
        """Names of what is missing. The app refuses to serve protected routes without these."""
        missing = []
        for name, value in [("SECRET_KEY", self.secret_key), ("VOICE_TOOL_SECRET", self.voice_secret),
                            ("ADMIN_PASSWORD", self.admin_password), ("CRON_SECRET", self.cron_secret)]:
            if len(value) < 16:
                missing.append(name)
        return missing

    def clash_on(self, cfg: dict, has_calendar: bool | None = None) -> bool:
        """Clash check needs both the switch and a real calendar."""
        calendar = bool(self.ics_url) if has_calendar is None else has_calendar
        return bool(cfg.get("features", {}).get("clash_check")) and calendar

    def quote_on(self, cfg: dict) -> bool:
        """Quote approval needs both the switch and Val's confirmed rates."""
        return bool(cfg.get("features", {}).get("quote_approval")) and bool(cfg.get("rates_confirmed"))

    def sms_on(self) -> bool:
        """Texts to Val need the switch, the Twilio details, his number and his written OK."""
        return bool(self.sms_alerts_enabled and self.val_sms_ok and self.val_mobile and self.twilio_number and self.twilio_account_sid and self.twilio_auth_token)

    def voice_can_hold(self, cfg: dict) -> bool:
        """Off by default: Volt saves the details and Val places any hold himself."""
        return bool(cfg.get("features", {}).get("voice_direct_hold"))

    def email_intake_on(self) -> bool:
        return bool(self.email_intake_enabled and self.booking_email and self.imap_user and self.imap_password and self.email_start_date)

    def business(self) -> dict:
        return json.loads(Path(self.config_path).read_text())


def load() -> Settings:
    return Settings()
