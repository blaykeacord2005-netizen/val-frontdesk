"""Texts to Val's own phone, and nobody else's. There's no 'to' argument on purpose: this code can't text a client.
Off until SMS_ALERTS_ENABLED, the Twilio details, VAL_MOBILE and VAL_SMS_OK are all set. Real delivery from a US local
number also needs A2P 10DLC registration; until that's approved, Twilio blocks these with error 30034 and the email alert still goes."""
from __future__ import annotations

import httpx

from .settings import Settings

_transport: httpx.BaseTransport | None = None  # tests swap in a fake


def send_to_val(s: Settings, body: str) -> str:
    """Returns sent, logged (dry run) or failed:<reason>. Never raises, so a text failure can't lose the inquiry."""
    if s.dry_run:
        return "logged"
    url = f"{s.twilio_api_base.rstrip('/')}/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
    try:
        with httpx.Client(timeout=10, transport=_transport) as client:
            resp = client.post(url, auth=(s.twilio_account_sid, s.twilio_auth_token), data={"To": s.val_mobile, "From": s.twilio_number, "Body": body})
    except httpx.HTTPError as exc:
        return f"failed:{type(exc).__name__}"
    if resp.status_code >= 300:
        try:
            return f"failed:{resp.json().get('code', resp.status_code)}"
        except ValueError:
            return f"failed:{resp.status_code}"
    return "sent"
