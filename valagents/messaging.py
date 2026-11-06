"""Outbox. Team alerts can send automatically. Anything to a client is a draft until a person approves it.
SMS and Instagram are never sent by code: approved items become copy-and-send, which avoids A2P registration
and Meta's ban on automated cold DMs."""
from __future__ import annotations

import smtplib
import sqlite3
from email.message import EmailMessage

from .db import audit, iso, utcnow
from .settings import Settings


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())[:200]


def draft(conn: sqlite3.Connection, channel: str, to: str | None, subject: str | None, body: str,
          booking_id: int | None, kind: str, dedupe: str | None = None, in_reply_to: str | None = None, inquiry_id: int | None = None) -> int | None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox(channel,to_addr,subject,body,booking_id,kind,status,dedupe_key,in_reply_to,inquiry_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (channel, to, _one_line(subject), body, booking_id, kind, "draft", dedupe, in_reply_to, inquiry_id, iso(utcnow())),
    )
    return cur.lastrowid if cur.rowcount else None


def alert(conn: sqlite3.Connection, s: Settings, to: str, subject: str, body: str, booking_id: int | None,
          dedupe: str | None = None, inquiry_id: int | None = None) -> str:
    """Internal team alert: to Val, Blayke, Max or Darrell only. Returns what happened. A mail failure is stored for retry
    and never raised, so it can't lose the inquiry that triggered it."""
    subject = _one_line(subject)
    if not to:
        conn.execute(
            "INSERT OR IGNORE INTO outbox(channel,to_addr,subject,body,booking_id,inquiry_id,kind,status,dedupe_key,created_at) "
            "VALUES('email','',?,?,?,?,'alert','no_address',?,?)",
            (subject, body, booking_id, inquiry_id, dedupe, iso(utcnow())),
        )
        return "no_address"
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox(channel,to_addr,subject,body,booking_id,inquiry_id,kind,status,dedupe_key,created_at) "
        "VALUES('email',?,?,?,?,?,'alert','approved',?,?)",
        (to, subject, body, booking_id, inquiry_id, dedupe, iso(utcnow())),
    )
    if not cur.rowcount:
        return "duplicate"
    try:
        return deliver(conn, s, cur.lastrowid)
    except Exception as exc:
        conn.execute("UPDATE outbox SET status='failed' WHERE id=?", (cur.lastrowid,))
        audit(conn, "system", "alert.failed", booking_id, {"outbox": cur.lastrowid, "error": type(exc).__name__})
        return "failed"


def approve(conn: sqlite3.Connection, s: Settings, outbox_id: int, actor: str) -> str:
    row = conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
    if row is None or row["status"] != "draft":
        raise ValueError("Nothing to approve")
    conn.execute("UPDATE outbox SET status='approved',decided_at=? WHERE id=?", (iso(utcnow()), outbox_id))
    audit(conn, actor, "outbox.approve", row["booking_id"], {"outbox": outbox_id})
    try:
        status = deliver(conn, s, outbox_id)
    except Exception as exc:
        # It didn't go out, so it goes back to waiting instead of looking sent.
        conn.execute("UPDATE outbox SET status='draft',decided_at=NULL WHERE id=?", (outbox_id,))
        audit(conn, "system", "outbox.send_failed", row["booking_id"], {"outbox": outbox_id, "error": type(exc).__name__})
        raise ValueError("Couldn't send that. It's still waiting as a draft.") from exc
    if row["inquiry_id"]:
        conn.execute("UPDATE inquiries SET val_replied_at=?,reply_method=? WHERE id=? AND val_replied_at IS NULL", (iso(utcnow()), status, row["inquiry_id"]))
    return status


def reject(conn: sqlite3.Connection, outbox_id: int, actor: str) -> None:
    cur = conn.execute("UPDATE outbox SET status='rejected',decided_at=? WHERE id=? AND status='draft'",
                       (iso(utcnow()), outbox_id))
    if cur.rowcount != 1:
        raise ValueError("Nothing to reject")
    audit(conn, actor, "outbox.reject", None, {"outbox": outbox_id})


def deliver(conn: sqlite3.Connection, s: Settings, outbox_id: int) -> str:
    row = conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
    if row["status"] != "approved":
        raise ValueError("Only approved items can leave the system")
    if row["channel"] == "sms_val":
        raise ValueError("Texts to Val only go through val_sms")
    if row["channel"] != "email":
        status = "copy_and_send"
    elif s.dry_run or not (s.smtp_user and s.smtp_password) or not row["to_addr"]:
        status = "logged"
    else:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.smtp_user, row["to_addr"], row["subject"] or "DJ Val"
        if row["in_reply_to"]:
            msg["In-Reply-To"] = _one_line(row["in_reply_to"])
            msg["References"] = _one_line(row["in_reply_to"])
        msg.set_content(row["body"])
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        status = "sent"
    conn.execute("UPDATE outbox SET status=?,sent_at=? WHERE id=?", (status, iso(utcnow()), outbox_id))
    return status
