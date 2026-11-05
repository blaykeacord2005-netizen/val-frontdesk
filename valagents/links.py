"""Signed links. Approval links are single-use and expire. Tampered links fail the signature check."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from datetime import timedelta

from .db import iso, parse, utcnow


class BadLink(ValueError):
    pass


def _sign(key: str, msg: str) -> str:
    raw = hmac.new(key.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mint(conn: sqlite3.Connection, key: str, booking_id: int, action: str, hours: int) -> str:
    if len(key) < 16:
        raise BadLink("SECRET_KEY is missing or too short")
    token_id = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO links(token_id,booking_id,action,expires_utc) VALUES(?,?,?,?)",
        (token_id, booking_id, action, iso(utcnow() + timedelta(hours=hours))),
    )
    return f"{token_id}.{_sign(key, token_id + '|' + action)}"


def check(conn: sqlite3.Connection, key: str, token: str, actions: tuple[str, ...]) -> dict:
    """Validates without consuming, so an email scanner opening the page can't use up the link."""
    try:
        token_id, sig = token.split(".", 1)
    except ValueError as exc:
        raise BadLink("This link isn't valid.") from exc
    row = conn.execute("SELECT * FROM links WHERE token_id=?", (token_id,)).fetchone()
    if row is None or row["action"] not in actions:
        raise BadLink("This link isn't valid.")
    if not hmac.compare_digest(sig, _sign(key, token_id + "|" + row["action"])):
        raise BadLink("This link isn't valid.")
    if row["used_at"]:
        raise BadLink("This link was already used.")
    if parse(row["expires_utc"]) <= utcnow():
        raise BadLink("This link has expired.")
    return dict(row)


def consume(conn: sqlite3.Connection, key: str, token: str, actions: tuple[str, ...]) -> dict:
    row = check(conn, key, token, actions)
    cur = conn.execute("UPDATE links SET used_at=? WHERE token_id=? AND used_at IS NULL", (iso(utcnow()), row["token_id"]))
    if cur.rowcount != 1:
        raise BadLink("This link was already used.")
    return row
