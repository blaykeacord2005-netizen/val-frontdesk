"""The front-desk queue. Every channel (voice, missed call, email) lands here first.
An inquiry is not a hold or a booking. It only becomes a hold when Val acts on it and holds are switched on."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from . import closer
from .availability import BadTime, conflicts, local_window
from .crm import clean_email, clean_phone, create_hold
from .db import audit, iso, utcnow
from .messaging import alert
from .settings import Settings

REQUIRED = {"org": "organization", "event_date": "date", "start_time": "start time", "end_time": "end time"}
LABELS = dict(REQUIRED, place="venue or campus", contact="a way to reach them")
OPEN = ("new", "needs_info", "ready", "review")


def missing_fields(f: dict) -> list[str]:
    missing = [label for key, label in REQUIRED.items() if not f.get(key)]
    if not (f.get("venue") or f.get("campus")):
        missing.append(LABELS["place"])
    if not (f.get("from_email") or f.get("from_phone")):
        missing.append(LABELS["contact"])
    return missing


def check_clash(conn: sqlite3.Connection, cfg: dict, f: dict, clash_on: bool, busy) -> str:
    if not clash_on:
        return "not_checked"
    if not (f.get("event_date") and f.get("start_time") and f.get("end_time")):
        return "not_enough_info"
    if busy is None:
        return "calendar_unreadable"
    try:
        start, end = local_window(f["event_date"], f["start_time"], f["end_time"])
    except BadTime:
        return "not_enough_info"
    return "conflict" if conflicts(conn, cfg, start, end, busy=busy) else "open"


def save(conn: sqlite3.Connection, s: Settings, cfg: dict, channel: str, f: dict, *, clash_on: bool = False, busy=None,
         status: str | None = None, external_ref: str | None = None, alert_val: bool = True, method: str = "rules") -> int:
    """Stores the inquiry, clash-checks it if switched on, and alerts Val. Sends nothing to the client."""
    missing = missing_fields(f)
    clash = check_clash(conn, cfg, f, clash_on, busy)
    state = status or ("needs_info" if missing else "ready")
    now = iso(utcnow())
    cur = conn.execute(
        "INSERT INTO inquiries(channel,status,from_name,from_email,from_phone,org,role,event_type,event_date,start_time,end_time,venue,campus,"
        "headcount,missing,notes,clash_status,subject,excerpt,method,external_ref,created_at,updated_at,received_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (channel, state, f.get("from_name"), f.get("from_email"), f.get("from_phone"), f.get("org"), f.get("role"), f.get("event_type"),
         f.get("event_date"), f.get("start_time"), f.get("end_time"), f.get("venue"), f.get("campus"), f.get("headcount"), json.dumps(missing),
         f.get("notes"), clash, f.get("subject"), f.get("excerpt"), method, external_ref, now, now, f.get("received_at") or now),
    )
    inquiry_id = cur.lastrowid
    audit(conn, channel, "inquiry.new", None, {"inquiry": inquiry_id, "status": state, "clash": clash})
    if alert_val:
        notify(conn, s, inquiry_id)
        closer.text_val(conn, s, inquiry_id)
    return inquiry_id


def get(conn: sqlite3.Connection, inquiry_id: int):
    row = conn.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
    if row is None:
        raise ValueError("Inquiry not found.")
    return row


CLASH_TEXT = {"not_checked": "not checked (clash check is off, look at your calendar)", "open": "looks open", "conflict": "CONFLICT with something on your calendar",
              "not_enough_info": "can't check yet, details missing", "calendar_unreadable": "couldn't read your calendar"}


def notify(conn: sqlite3.Connection, s: Settings, inquiry_id: int, prefix: str = "New inquiry") -> None:
    q = get(conn, inquiry_id)
    who = q["org"] or q["from_name"] or q["from_phone"] or q["from_email"] or "Unknown"
    missing = json.loads(q["missing"])
    lines = [f"Channel: {q['channel']}", f"From: {q['from_name'] or '-'} · {q['from_email'] or '-'} · {q['from_phone'] or '-'}",
             f"Org: {q['org'] or '-'}", f"Event: {q['event_type'] or '-'}", f"Date: {q['event_date'] or '-'}  {q['start_time'] or '?'} to {q['end_time'] or '?'}",
             f"Place: {q['venue'] or q['campus'] or '-'}", f"Headcount: {q['headcount'] or '-'}", f"Date check: {CLASH_TEXT.get(q['clash_status'], q['clash_status'])}",
             f"Still missing: {', '.join(missing) or 'nothing'}"]
    if q["excerpt"]:
        lines.append(f"\nTheir words:\n{q['excerpt']}")
    lines.append(f"\nNothing has been sent to them. Review: {closer.review_url(conn, s, inquiry_id)}")
    state = alert(conn, s, s.alert_val, f"{prefix}: {who}", "\n".join(lines), None, f"{prefix}-{inquiry_id}-{q['updated_at']}", inquiry_id=inquiry_id)
    if state != "duplicate":
        closer.record_alert(conn, inquiry_id, state)


def merge(conn: sqlite3.Connection, s: Settings, cfg: dict, inquiry_id: int, f: dict, *, clash_on: bool = False, busy=None) -> None:
    """A client reply fills in blanks. A different value goes to notes for Val instead of overwriting."""
    q = dict(get(conn, inquiry_id))
    notes = [q["notes"]] if q["notes"] else []
    for key in ("org", "role", "event_type", "event_date", "start_time", "end_time", "venue", "campus", "headcount", "from_phone", "from_email"):
        new = f.get(key)
        if new in (None, ""):
            continue
        if q[key] in (None, ""):
            q[key] = new
        elif str(q[key]).strip().lower() != str(new).strip().lower():
            notes.append(f"Reply says {key} is '{new}', on file '{q[key]}'")
    if f.get("excerpt"):
        q["excerpt"] = ((q["excerpt"] or "") + "\n--- reply ---\n" + f["excerpt"])[-2000:]
    missing = missing_fields(q)
    clash = check_clash(conn, cfg, q, clash_on, busy)
    conn.execute(
        "UPDATE inquiries SET org=?,role=?,event_type=?,event_date=?,start_time=?,end_time=?,venue=?,campus=?,headcount=?,from_phone=?,from_email=?,"
        "missing=?,notes=?,clash_status=?,excerpt=?,status=?,updated_at=? WHERE id=?",
        (q["org"], q["role"], q["event_type"], q["event_date"], q["start_time"], q["end_time"], q["venue"], q["campus"], q["headcount"], q["from_phone"], q["from_email"],
         json.dumps(missing), "\n".join(notes) or None, clash, q["excerpt"], "needs_info" if missing else "ready", iso(utcnow()), inquiry_id),
    )
    audit(conn, "email", "inquiry.reply", None, {"inquiry": inquiry_id})
    notify(conn, s, inquiry_id, "Client replied")


def open_list(conn: sqlite3.Connection):
    return conn.execute(f"SELECT * FROM inquiries WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY id", OPEN).fetchall()


def close(conn: sqlite3.Connection, inquiry_id: int, status: str) -> None:
    q = get(conn, inquiry_id)
    if q["status"] not in OPEN:
        raise ValueError("That inquiry is already closed.")
    conn.execute("UPDATE inquiries SET status=?,updated_at=? WHERE id=?", (status, iso(utcnow()), inquiry_id))
    audit(conn, "val", "inquiry.close", None, {"inquiry": inquiry_id, "status": status})


def promote_to_hold(conn: sqlite3.Connection, s: Settings, cfg: dict, inquiry_id: int, busy=None) -> dict:
    """Val chose to hold this date. Only reachable with holds switched on. Re-checks the calendar inside create_hold."""
    q = get(conn, inquiry_id)
    if q["status"] != "ready":
        raise ValueError("This inquiry isn't ready to hold.")
    start, end = local_window(q["event_date"], q["start_time"], q["end_time"])
    result = create_hold(conn, s, cfg, name=q["from_name"] or "Client", phone=clean_phone(q["from_phone"]), email=clean_email(q["from_email"]),
                         org=q["org"], role=q["role"], event_type=q["event_type"] or "party", start=start, end=end, venue=q["venue"],
                         headcount=q["headcount"], package=None, campus=q["campus"], source=q["channel"], busy=busy)
    conn.execute("UPDATE inquiries SET status='promoted',booking_id=?,updated_at=? WHERE id=?", (result["booking_id"], iso(utcnow()), inquiry_id))
    return result


def valid_date(value: str | None) -> str | None:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None
