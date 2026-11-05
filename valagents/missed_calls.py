"""Missed-call handling. Twilio reports how each call ended. If nobody talked to Volt, Val gets an email with the
number to call back. No text-back: that waits for A2P approval."""
from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3

from . import inquiries
from .crm import clean_phone
from .db import iso, utcnow
from .settings import Settings

MISSED = {"no-answer", "busy", "failed", "canceled"}
HANGUP_SECONDS = 15


def valid_signature(auth_token: str, url: str, params: dict, signature: str | None) -> bool:
    """Twilio's documented scheme: HMAC-SHA1 of the URL plus sorted params, base64 encoded. No token means no entry."""
    if not auth_token or not signature:
        return False
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def classify(status: str, duration: int) -> str:
    if status in MISSED:
        return "missed"
    if status == "completed":
        return "hung_up_fast" if duration < HANGUP_SECONDS else "answered_by_volt"
    return "in_progress"


def handle_status(conn: sqlite3.Connection, s: Settings, cfg: dict, params: dict) -> dict:
    sid = params.get("CallSid", "")[:64]
    status = params.get("CallStatus", "").lower()
    try:
        duration = int(params.get("CallDuration") or 0)
    except ValueError:
        duration = 0
    if not sid or not status:
        return {"ok": False, "error": "missing call details"}
    outcome = classify(status, duration)
    if outcome == "in_progress":
        return {"ok": True, "outcome": outcome}
    if s.twilio_number and params.get("To") and clean_phone(params["To"]) != clean_phone(s.twilio_number):
        return {"ok": True, "outcome": "ignored_wrong_number"}
    raw_from = params.get("From", "")
    phone = clean_phone(raw_from)
    cur = conn.execute("INSERT OR IGNORE INTO calls(call_sid,from_phone,status,duration,outcome,at) VALUES(?,?,?,?,?,?)",
                       (sid, phone or "hidden", status, duration, outcome, iso(utcnow())))
    if cur.rowcount == 0:
        return {"ok": True, "outcome": outcome, "duplicate": True}
    if outcome in ("missed", "hung_up_fast"):
        note = "Call dropped or was not answered" if outcome == "missed" else f"Caller hung up after {duration} seconds"
        inquiries.save(conn, s, cfg, "missed_call", {"from_phone": phone, "notes": note,
                                                     "excerpt": f"{note}. Number: {phone or 'hidden caller ID'}. Call them back."},
                       status="needs_info", external_ref=f"call:{sid}")
    return {"ok": True, "outcome": outcome}
