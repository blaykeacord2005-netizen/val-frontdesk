"""The Closer: catches inquiries Val missed or is slow on, and measures how fast they get answered.
It never sends anything to a client. Every client message is a draft until Val approves it, and it never touches his calendar.
Target from GigSalad's data: reply in under an hour (they report a 25% better chance of booking). That's a target, not a promise."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import val_sms
from .db import audit, iso, parse, utcnow
from .links import BadLink, mint
from .messaging import alert, deliver, draft
from .settings import TZ, Settings

OPEN_FOR_REPLY = ("new", "needs_info", "ready")
DRAFT_WINDOW_DAYS = 7
DAILY_HEADER = ["Date", "Inquiries", "Missed calls", "Avg seconds to alert Val", "Avg minutes to Val's first reply", "Replied under 1 hour", "Still unanswered", "Bookings won"]
# What a Closer draft may never say. Client-supplied text gets checked against this too.
BANNED = re.compile(r"\$|\b(prices?|pricing|rates?|costs?|quotes?|fees?|free|available|availability|open|holds?|held|confirm\w*|deposits?|guarantee\w*)\b", re.I)
LINKISH = re.compile(r"https?://\S+|www\.\S+|\b\S+\.(?:com|net|org|ly|co|io|me)\b\S*", re.I)


def slow_hours(cfg: dict) -> float:
    return float(cfg.get("closer", {}).get("slow_reply_hours", 2))


def target_minutes(cfg: dict) -> int:
    return int(cfg.get("closer", {}).get("first_response_target_minutes", 60))


def arrived(q) -> str:
    return q["received_at"] or q["created_at"]


def review_url(conn: sqlite3.Connection, s: Settings, inquiry_id: int) -> str:
    """Opening this page changes nothing, so text previews and email scanners can't approve anything. Only the buttons act."""
    try:
        return f"{s.public_url}/r/{mint(conn, s.secret_key, inquiry_id, 'review_inquiry', 24 * 14)}"
    except BadLink:
        return f"{s.public_url}/admin"


def record_alert(conn: sqlite3.Connection, inquiry_id: int, state: str) -> None:
    """alerted_at is only set when something really left. A dry-run log or a missing address isn't an alert."""
    conn.execute("UPDATE inquiries SET alert_state=?,alerted_at=CASE WHEN ?='sent' THEN COALESCE(alerted_at,?) ELSE alerted_at END WHERE id=?",
                 (state, state, iso(utcnow()), inquiry_id))


def _clean(value, limit: int = 40) -> str:
    return " ".join(LINKISH.sub("", str(value or "")).split())[:limit].strip(" ,.-")


def _pretty_date(value: str | None) -> str:
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return ""
    return f"{d:%a %b} {d.day}"


def sms_text(q, link: str) -> str:
    who = _clean(q["org"] or q["from_name"]) or q["from_phone"] or _clean(q["from_email"]) or "Unknown"
    if q["channel"] == "missed_call":
        return f"DJ Val missed call: {who}. Call back, or review: {link}"[:320]
    when = _pretty_date(q["event_date"]) or "no date yet"
    where = _clean(q["venue"] or q["campus"]) or "no venue yet"
    return f"DJ Val inquiry: {who}, {when}, {where}. Review: {link}"[:320]


def text_val(conn: sqlite3.Connection, s: Settings, inquiry_id: int) -> str:
    """One short text per new inquiry. Skipped until every switch and written OK is in place."""
    if not s.sms_on():
        return "off"
    q = conn.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
    body = sms_text(q, review_url(conn, s, inquiry_id))
    cur = conn.execute("INSERT OR IGNORE INTO outbox(channel,to_addr,subject,body,inquiry_id,kind,status,dedupe_key,created_at) VALUES('sms_val',?,NULL,?,?,'alert','approved',?,?)",
                       (s.val_mobile, body, inquiry_id, f"sms-{inquiry_id}", iso(utcnow())))
    if not cur.rowcount:
        return "duplicate"
    result = val_sms.send_to_val(s, body)
    state = "failed" if result.startswith("failed") else result
    conn.execute("UPDATE outbox SET status=?,sent_at=? WHERE id=?", (state, iso(utcnow()), cur.lastrowid))
    audit(conn, "closer", "sms.val", None, {"inquiry": inquiry_id, "result": result})
    if state == "sent":
        record_alert(conn, inquiry_id, "sent")
    return result


def mark_replied(conn: sqlite3.Connection, inquiry_id: int, method: str, at: str | None = None) -> bool:
    cur = conn.execute("UPDATE inquiries SET val_replied_at=?,reply_method=? WHERE id=? AND val_replied_at IS NULL", (at or iso(utcnow()), method, inquiry_id))
    if cur.rowcount:
        audit(conn, "val", "inquiry.replied", None, {"inquiry": inquiry_id, "method": method})
    return bool(cur.rowcount)


def _safe(value, limit: int = 60) -> str:
    text = _clean(value, limit)
    return "" if not text or BANNED.search(text) else text


def followup_body(q) -> str:
    """Built only from fields on the inquiry. A field holding a price or a promise gets left out, not repeated."""
    first = re.sub(r"[^A-Za-z'-]", "", (q["from_name"] or "").split(" ")[0])[:30]
    hello = f"Hey {first}, it's Val." if first else "Hey, it's Val."
    if q["channel"] == "missed_call" and not q["event_date"]:
        return f"{hello} Sorry I missed your call. If this is about an event, send me the date, time and venue and I'll follow up personally."
    org = _safe(q["org"])
    day = _pretty_date(q["event_date"])
    place = _safe(q["venue"] or q["campus"])
    count = q["headcount"] if isinstance(q["headcount"], int) and q["headcount"] > 0 else None
    event = f"for {org}" if org else "for your event"
    details = event + (f" on {day}" if day else "") + (f" at {place}" if place else "") + (f" with about {count} people" if count else "")
    missing = [m for m in json.loads(q["missing"] or "[]") if m != "a way to reach them"]
    ask = f"Could you send me the {', '.join(missing)}?" if missing else "I'll follow up with you personally on next steps."
    body = f"{hello} Thanks for reaching out about DJing {details}. {ask}"
    return body if not BANNED.search(body) else f"{hello} Thanks for reaching out. I'll follow up with you personally on next steps."


def _pending_draft(conn: sqlite3.Connection, inquiry_id: int):
    return conn.execute("SELECT id FROM outbox WHERE inquiry_id=? AND status='draft' ORDER BY id LIMIT 1", (inquiry_id,)).fetchone()


def _make_followup(conn: sqlite3.Connection, q) -> int | None:
    if q["from_email"]:
        seen = conn.execute("SELECT message_id FROM email_seen WHERE inquiry_id=? AND message_id IS NOT NULL ORDER BY at LIMIT 1", (q["id"],)).fetchone()
        subject = f"Re: {q['subject']}" if q["subject"] else "DJ Val booking"
        return draft(conn, "email", q["from_email"], subject, followup_body(q), None, "closer_followup", f"closer-{q['id']}", in_reply_to=seen["message_id"] if seen else None, inquiry_id=q["id"])
    if q["from_phone"]:
        # A text draft is copy-and-send only: Val texts it himself, so no A2P step and no automated client text.
        return draft(conn, "text", q["from_phone"], None, followup_body(q), None, "closer_followup", f"closer-{q['id']}", inquiry_id=q["id"])
    return None


def sweep(conn: sqlite3.Connection, s: Settings, cfg: dict, now: datetime) -> dict:
    """Runs every 15 minutes with the email check. Safe to run over and over: dedupe keys stop repeats."""
    out = {"drafted": 0, "nudged": 0, "no_contact": 0, "alerts_retried": 0}
    cutoff, oldest = iso(now - timedelta(hours=slow_hours(cfg))), iso(now - timedelta(days=DRAFT_WINDOW_DAYS))
    marks = ",".join("?" * len(OPEN_FOR_REPLY))
    rows = conn.execute(f"SELECT * FROM inquiries WHERE status IN ({marks}) AND val_replied_at IS NULL AND COALESCE(received_at,created_at)<=? AND COALESCE(received_at,created_at)>=? ORDER BY id",
                        (*OPEN_FOR_REPLY, cutoff, oldest)).fetchall()
    for q in rows:
        if conn.execute("SELECT 1 FROM outbox WHERE dedupe_key=?", (f"closer-nudge-{q['id']}",)).fetchone():
            continue
        made = None if _pending_draft(conn, q["id"]) else _make_followup(conn, q)
        reachable = bool(q["from_email"] or q["from_phone"])
        out["drafted"] += 1 if made else 0
        out["no_contact"] += 0 if reachable else 1
        who = q["org"] or q["from_name"] or q["from_phone"] or q["from_email"] or "Unknown"
        line = f"A draft reply is ready. Nothing goes out unless you approve it." if reachable else "They left no way to reach them, so there's no draft."
        alert(conn, s, s.alert_val, f"Still unanswered after {slow_hours(cfg):g} hours: {who}", f"{line}\nReview: {review_url(conn, s, q['id'])}", None, f"closer-nudge-{q['id']}", inquiry_id=q["id"])
        out["nudged"] += 1
    for row in conn.execute("SELECT id,inquiry_id FROM outbox WHERE kind='alert' AND status='failed' AND channel='email' AND created_at>=?", (iso(now - timedelta(hours=24)),)).fetchall():
        conn.execute("UPDATE outbox SET status='approved' WHERE id=?", (row["id"],))
        try:
            state = deliver(conn, s, row["id"])
        except Exception:
            conn.execute("UPDATE outbox SET status='failed' WHERE id=?", (row["id"],))
            continue
        out["alerts_retried"] += 1
        if row["inquiry_id"]:
            record_alert(conn, row["inquiry_id"], state)
    return out


def _local_day(value: str) -> date:
    return parse(value).astimezone(TZ).date()


def _seconds(start: str, end: str) -> float:
    return max(0.0, (parse(end) - parse(start)).total_seconds())


def counts_by_day(conn: sqlite3.Connection, now: datetime) -> dict[date, dict]:
    today = now.astimezone(TZ).date()
    days: dict[date, dict] = {}

    def day(d: date) -> dict:
        return days.setdefault(d, {"inquiries": 0, "missed": 0, "alert": [], "reply": [], "under": 0, "open": 0, "won": 0})

    for q in conn.execute("SELECT * FROM inquiries").fetchall():
        row = day(_local_day(arrived(q)))
        row["inquiries"] += 1
        if q["alerted_at"]:
            row["alert"].append(_seconds(arrived(q), q["alerted_at"]))
        # A dry-run "send" never reached the client, so it doesn't count as a reply.
        if q["val_replied_at"] and q["reply_method"] != "logged":
            minutes = _seconds(arrived(q), q["val_replied_at"]) / 60
            row["reply"].append(minutes)
            row["under"] += 1 if minutes < 60 else 0
        elif q["status"] in OPEN_FOR_REPLY:
            row["open"] += 1
    for c in conn.execute("SELECT at FROM calls WHERE outcome IN ('missed','hung_up_fast')").fetchall():
        day(_local_day(c["at"]))["missed"] += 1
    for b in conn.execute("SELECT deposit_at FROM bookings WHERE deposit_at IS NOT NULL AND stage IN ('confirmed','played')").fetchall():
        day(_local_day(b["deposit_at"]))["won"] += 1
    day(today)
    return days


def daily_rows(conn: sqlite3.Connection, now: datetime) -> list[list]:
    """One row per day, zero days included. A blank average means no data. It doesn't mean zero minutes."""
    days = counts_by_day(conn, now)
    first, today = min(days), now.astimezone(TZ).date()
    rows = [DAILY_HEADER]
    d = first
    while d <= today:
        r = days.get(d) or {"inquiries": 0, "missed": 0, "alert": [], "reply": [], "under": 0, "open": 0, "won": 0}
        rows.append([d.isoformat(), r["inquiries"], r["missed"], round(sum(r["alert"]) / len(r["alert"]), 1) if r["alert"] else "",
                     round(sum(r["reply"]) / len(r["reply"]), 1) if r["reply"] else "", r["under"], r["open"], r["won"]])
        d += timedelta(days=1)
    return rows


def today_report(conn: sqlite3.Connection, now: datetime) -> dict:
    return dict(zip(DAILY_HEADER, daily_rows(conn, now)[-1]))


def clamp_received(value: datetime | None, now: datetime) -> str | None:
    """An email's Date header is written by the sender, so it's only trusted if it's in the past and recent."""
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return iso(value) if now - timedelta(days=30) <= value <= now else None
