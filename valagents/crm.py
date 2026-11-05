"""Pipeline logic. new > held > quoted > confirmed > played > (rebook). Also lost and expired.
The voice agent can only reach create_hold and take_message: no confirm or payment path exists for it."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

from .availability import conflicts
from .db import audit, iso, parse, utcnow
from .links import mint
from .messaging import alert, draft
from .money import QuoteError, build_quote, dollars
from .settings import TZ, Settings


class CrmError(ValueError):
    pass


def clean_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return "+1" + digits if len(digits) == 10 else None


def clean_email(raw: str | None) -> str | None:
    if raw and re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}", raw.strip()):
        return raw.strip().lower()
    return None


def upsert_contact(conn: sqlite3.Connection, name: str, phone: str | None, email: str | None, org: str | None,
                   role: str | None, source: str) -> int:
    existing = conn.execute("SELECT id FROM contacts WHERE phone=?", (phone,)).fetchone() if phone else None
    if existing:
        conn.execute(
            "UPDATE contacts SET name=COALESCE(NULLIF(?,''),name), email=COALESCE(?,email), "
            "org=COALESCE(NULLIF(?,''),org), role=COALESCE(NULLIF(?,''),role) WHERE id=?",
            (name, email, org, role, existing["id"]),
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO contacts(name,phone,email,org,role,source,created_at) VALUES(?,?,?,?,?,?,?)",
        (name, phone, email, org, role, source, iso(utcnow())),
    )
    return cur.lastrowid


def local(dt_iso: str) -> datetime:
    return parse(dt_iso).astimezone(TZ)


def pretty_when(b) -> str:
    s, e = local(b["start_utc"]), local(b["end_utc"])
    return f"{s.strftime('%a %b')} {s.day}, {s.strftime('%-I:%M%p').lower()}–{e.strftime('%-I:%M%p').lower()}"


def create_hold(conn: sqlite3.Connection, s: Settings, cfg: dict, *, name: str, phone: str | None, email: str | None,
                org: str, role: str | None, event_type: str, start: datetime, end: datetime, venue: str | None,
                headcount: int | None, package: str | None, campus: str | None, source: str, busy=None,
                notes: str | None = None) -> dict:
    now = utcnow()
    if start <= now:
        raise CrmError("That date has already passed.")
    if start > now + timedelta(days=int(cfg["booking_window_days"])):
        raise CrmError("That date is too far out to hold.")
    if not name.strip() or not org.strip():
        raise CrmError("Need a name and an organization.")
    if not (phone or email):
        raise CrmError("Need a phone number or an email to reach them.")
    if package and package not in cfg["packages"]:
        raise CrmError("Unknown package.")
    # Take the database write lock before checking, so two simultaneous calls can't both hold one night.
    conn.execute("BEGIN IMMEDIATE")
    try:
        clash = conflicts(conn, cfg, start, end, busy=busy)
        if clash:
            raise CrmError("That time isn't open.")
        contact = upsert_contact(conn, name.strip(), phone, email, org.strip(), role, source)
        miles = cfg["campus_miles"].get(campus) if campus else None
        cur = conn.execute(
            "INSERT INTO bookings(contact_id,org,event_type,start_utc,end_utc,venue,headcount,package,campus,miles,stage,"
            "hold_expires_utc,notes,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'held',?,?,?,?,?)",
            (contact, org.strip(), event_type, iso(start), iso(end), venue, headcount, package or "chapter_night", campus,
             miles, iso(now + timedelta(hours=int(cfg["hold_hours"]))), notes, source, iso(now), iso(now)),
        )
        booking_id = cur.lastrowid
        audit(conn, source, "booking.hold", booking_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    b = get(conn, booking_id)
    approve_link = f"{s.public_url}/a/{mint(conn, s.secret_key, booking_id, 'approve_quote', int(cfg['hold_hours']))}"
    decline_link = f"{s.public_url}/a/{mint(conn, s.secret_key, booking_id, 'decline', int(cfg['hold_hours']))}"
    alert(conn, s, s.alert_val, f"New hold: {org} · {pretty_when(b)}",
          f"{name} ({role or 'role unknown'}) from {org} wants {pretty_when(b)}.\nType: {event_type}\nVenue: {venue or 'unknown'}\n"
          f"Headcount: {headcount or 'unknown'}\nCampus: {campus or 'unknown'}\nPhone: {phone or '-'}\nEmail: {email or '-'}\n"
          f"Notes: {notes or '-'}\n\nThe hold expires in {cfg['hold_hours']} hours.\nQuote it: {approve_link}\nDecline it: {decline_link}\n",
          booking_id, dedupe=f"hold-alert-{booking_id}")
    return {"booking_id": booking_id, "hold_expires_utc": b["hold_expires_utc"]}


def get(conn: sqlite3.Connection, booking_id: int):
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if row is None:
        raise CrmError("Booking not found.")
    return row


def contact_of(conn: sqlite3.Connection, b):
    return conn.execute("SELECT * FROM contacts WHERE id=?", (b["contact_id"],)).fetchone()


def set_minutes(b) -> int:
    return int((parse(b["end_utc"]) - parse(b["start_utc"])).total_seconds() // 60)


def approve_quote(conn: sqlite3.Connection, s: Settings, cfg: dict, booking_id: int, actor: str, busy=None) -> dict:
    """Val tapped approve. Re-check everything: rates, stage and calendar may have changed since the hold."""
    if not cfg.get("rates_confirmed"):
        raise CrmError("Val's rates aren't confirmed yet, so no quote can go out. Set rates_confirmed in the config.")
    b = get(conn, booking_id)
    if b["stage"] != "held":
        raise CrmError(f"This booking is {b['stage']}, not waiting on a quote.")
    if conflicts(conn, cfg, parse(b["start_utc"]), parse(b["end_utc"]), exclude=booking_id, busy=busy):
        raise CrmError("That date is no longer free. Nothing was sent.")
    if b["campus"] and b["miles"] is None:
        raise CrmError("Distance unknown, so travel can't be priced.")
    try:
        q = build_quote(cfg, b["package"], set_minutes(b), b["miles"])
    except QuoteError as exc:
        raise CrmError(str(exc)) from exc
    now = iso(utcnow())
    conn.execute("UPDATE bookings SET stage='quoted',quote_cents=?,deposit_cents=?,quoted_at=?,hold_expires_utc=NULL,updated_at=? WHERE id=?",
                 (q.total_cents, q.deposit_cents, now, now, booking_id))
    audit(conn, actor, "booking.quote", booking_id, {"total_cents": q.total_cents})
    c = contact_of(conn, b)
    lines = [f"{q.label}: {dollars(q.base_cents)}"]
    if q.overtime_cents:
        lines.append(f"Extra time: {dollars(q.overtime_cents)}")
    if q.travel_cents:
        lines.append(f"Travel: {dollars(q.travel_cents)}")
    body = (f"Hey {c['name'].split()[0]}, it's Val. I can do {pretty_when(b)} for {b['org']}.\n\n" + "\n".join(lines) +
            f"\nTotal: {dollars(q.total_cents)}\n\n{dollars(q.deposit_cents)} deposit holds your date. "
            "The balance is due the day before. Want me to send the agreement and deposit link?")
    channel, to = ("email", c["email"]) if c["email"] else ("text", c["phone"])
    outbox_id = draft(conn, channel, to, f"DJ Val: {b['org']} quote", body, booking_id, "quote", f"quote-{booking_id}")
    return {"quote": q, "outbox_id": outbox_id}


def decline(conn: sqlite3.Connection, booking_id: int, actor: str) -> None:
    b = get(conn, booking_id)
    if b["stage"] not in ("held", "quoted"):
        raise CrmError(f"Can't decline a booking that is {b['stage']}.")
    conn.execute("UPDATE bookings SET stage='lost',hold_expires_utc=NULL,updated_at=? WHERE id=?", (iso(utcnow()), booking_id))
    audit(conn, actor, "booking.decline", booking_id)


def mark_deposit(conn: sqlite3.Connection, booking_id: int, actor: str) -> None:
    """A person records that the deposit landed. Code never moves money."""
    b = get(conn, booking_id)
    if b["stage"] != "quoted":
        raise CrmError(f"Can't record a deposit on a booking that is {b['stage']}.")
    now = iso(utcnow())
    conn.execute("UPDATE bookings SET stage='confirmed',deposit_at=?,updated_at=? WHERE id=?", (now, now, booking_id))
    audit(conn, actor, "booking.deposit", booking_id)


def merge_details(conn: sqlite3.Connection, booking_id: int, updates: dict) -> dict:
    b = get(conn, booking_id)
    details = json.loads(b["details"] or "{}")
    details.update(updates)
    conn.execute("UPDATE bookings SET details=?,updated_at=? WHERE id=?", (json.dumps(details), iso(utcnow()), booking_id))
    return details
