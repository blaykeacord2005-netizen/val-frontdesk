"""Backend for Volt, the inbound voice agent. ElevenLabs runs the conversation over a Twilio number; these are the
only actions it can take. There's deliberately no tool to confirm a booking, quote a price or take payment.
With clash check and quote approval switched off, Volt takes details only: it can't say a date is free or hold one."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from . import inquiries
from .availability import BadTime, conflicts, local_window
from .crm import CrmError, clean_email, clean_phone, create_hold
from .db import audit
from .messaging import alert
from .settings import TZ, Settings


def init_payload(cfg: dict, caller_id: str | None, now: datetime) -> dict:
    local = now.astimezone(TZ)
    return {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": {
            "today": f"{local.strftime('%A, %B')} {local.day}, {local.year}",
            "caller_number": clean_phone(caller_id) or "unknown",
            "agent_name": cfg["agent_name"],
            "dj_name": cfg["dj_name"],
        },
    }


def availability_off() -> dict:
    return {"ok": True, "available": None, "say": "You can't check dates yet. Take the details with save_inquiry and say Val will confirm the date himself."}


def check_availability(conn: sqlite3.Connection, cfg: dict, day: str, start: str, end: str, busy=None) -> dict:
    try:
        s, e = local_window(day, start, end)
    except BadTime as exc:
        return {"ok": False, "error": str(exc)}
    if conflicts(conn, cfg, s, e, busy=busy):
        return {"ok": True, "available": False, "say": "That time isn't open. Ask for another date, or offer to have Val follow up."}
    return {"ok": True, "available": True, "say": "It looks open. Offer to place a 48-hour hold while Val reviews it."}


def _fields(data: dict) -> dict:
    day = inquiries.valid_date(data.get("date"))
    times = {}
    for key, name in (("start_time", "start_time"), ("end_time", "end_time")):
        value = str(data.get(key) or "")
        times[name] = value if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) else None
    headcount = data.get("headcount")
    return {"from_name": str(data.get("caller_name") or "")[:80] or None, "from_phone": clean_phone(data.get("phone")), "from_email": clean_email(data.get("email")),
            "org": str(data.get("organization") or "")[:120].strip() or None, "role": str(data.get("role") or "")[:60] or None,
            "event_type": str(data.get("event_type") or "")[:60] or None, "event_date": day, **times,
            "venue": str(data.get("venue") or "")[:160] or None, "campus": str(data.get("campus") or "")[:60] or None,
            "headcount": int(headcount) if str(headcount or "").isdigit() else None, "notes": str(data.get("notes") or "")[:500] or None}


def save_inquiry(conn: sqlite3.Connection, s: Settings, cfg: dict, data: dict) -> dict:
    """Takes a message-with-details. Holds nothing. Val gets the email alert."""
    fields = _fields(data)
    if not any(fields[k] for k in ("from_phone", "from_email")):
        return {"ok": False, "error": "Get a phone number or email first."}
    inquiry_id = inquiries.save(conn, s, cfg, "voice", fields)
    return {"ok": True, "inquiry_id": inquiry_id, "held": False,
            "say": "Details saved. Nothing is held or confirmed yet. Val will reach out himself about the date and pricing."}


def hold_date(conn: sqlite3.Connection, s: Settings, cfg: dict, data: dict, busy=None) -> dict:
    try:
        start, end = local_window(str(data.get("date", "")), str(data.get("start_time", "")), str(data.get("end_time", "")))
        headcount = data.get("headcount")
        result = create_hold(
            conn, s, cfg, name=str(data.get("caller_name", ""))[:80], phone=clean_phone(data.get("phone")), email=clean_email(data.get("email")),
            org=str(data.get("organization", ""))[:120], role=str(data.get("role") or "")[:60] or None,
            event_type=str(data.get("event_type") or "party")[:60], start=start, end=end, venue=str(data.get("venue") or "")[:160] or None,
            headcount=int(headcount) if str(headcount or "").isdigit() else None,
            package=None, campus=str(data.get("campus") or "") or None, source="voice", busy=busy,
            notes=str(data.get("notes") or "")[:500] or None,
        )
    except (BadTime, CrmError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "booking_id": result["booking_id"],
            "say": f"The date is on hold for {cfg['hold_hours']} hours, and it isn't confirmed yet. Val will reach out personally with pricing."}


def take_message(conn: sqlite3.Connection, s: Settings, data: dict) -> dict:
    name = str(data.get("caller_name") or "Unknown")[:80]
    phone = clean_phone(data.get("phone"))
    message = str(data.get("message") or "")[:1000]
    if not message.strip():
        return {"ok": False, "error": "Need a message."}
    alert(conn, s, s.alert_val, f"Message from {name}", f"From: {name}\nPhone: {phone or 'not given'}\n\n{message}", None)
    audit(conn, "voice", "voice.message", None, {"name": name})
    return {"ok": True, "say": "Message passed to Val."}


def post_call(conn: sqlite3.Connection, s: Settings, data: dict) -> dict:
    """End-of-call summary. Stored as an audit line and sent to Val. Summary only, no audio."""
    summary = str(((data.get("data") or {}).get("analysis") or {}).get("transcript_summary") or data.get("summary") or "")[:2000]
    call_id = str((data.get("data") or {}).get("conversation_id") or data.get("conversation_id") or "")[:80]
    if summary:
        audit(conn, "voice", "voice.call", None, {"conversation_id": call_id, "summary": summary})
        alert(conn, s, s.alert_val, "Volt call summary", summary, None, f"call-{call_id}" if call_id else None)
    return {"ok": True}
