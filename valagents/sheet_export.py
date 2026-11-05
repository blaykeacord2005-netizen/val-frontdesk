"""Gig tracker: a one-way copy from the CRM (SQLite) to Google Sheets. This module only writes.
It has no code path that reads a sheet, so edits made in the sheet never reach the CRM and get overwritten on the next run."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .crm import local
from .db import iso, utcnow
from .money import dollars

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GIG_HEADER = ["Booking #", "Stage", "Date", "Start", "End", "Org", "Contact", "Phone", "Email", "Venue", "Campus", "Headcount", "Package", "Quote", "Deposit", "Source", "Updated (UTC)"]
INQ_HEADER = ["Inquiry #", "Status", "Channel", "From", "Email", "Phone", "Org", "Event", "Date", "Start", "End", "Venue", "Campus", "Headcount", "Date check", "Missing", "Received (UTC)"]
GIG_TAB = "Gigs (auto, do not edit)"
INQ_TAB = "Inquiries (auto, do not edit)"


def gig_rows(conn: sqlite3.Connection) -> list[list]:
    rows = [GIG_HEADER]
    query = ("SELECT b.*, c.name, c.phone, c.email FROM bookings b LEFT JOIN contacts c ON c.id=b.contact_id "
             "WHERE b.stage NOT IN ('lost','expired') ORDER BY b.start_utc")
    for b in conn.execute(query).fetchall():
        s, e = local(b["start_utc"]), local(b["end_utc"])
        rows.append([b["id"], b["stage"], s.strftime("%Y-%m-%d"), s.strftime("%H:%M"), e.strftime("%H:%M"), b["org"] or "", b["name"] or "", b["phone"] or "", b["email"] or "",
                     b["venue"] or "", b["campus"] or "", b["headcount"] or "", b["package"] or "", dollars(b["quote_cents"]) if b["quote_cents"] else "",
                     dollars(b["deposit_cents"]) if b["deposit_cents"] else "", b["source"] or "", b["updated_at"]])
    return rows


def inquiry_rows(conn: sqlite3.Connection) -> list[list]:
    rows = [INQ_HEADER]
    for q in conn.execute("SELECT * FROM inquiries ORDER BY id").fetchall():
        rows.append([q["id"], q["status"], q["channel"], q["from_name"] or "", q["from_email"] or "", q["from_phone"] or "", q["org"] or "", q["event_type"] or "",
                     q["event_date"] or "", q["start_time"] or "", q["end_time"] or "", q["venue"] or "", q["campus"] or "", q["headcount"] or "", q["clash_status"],
                     ", ".join(json.loads(q["missing"] or "[]")), q["created_at"]])
    return rows


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def service_account_token(info: dict, client: httpx.Client) -> str:
    """Google's documented service-account flow: a signed JWT swapped for a short-lived access token."""
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": info["client_email"], "scope": SCOPE, "aud": info["token_uri"], "iat": now, "exp": now + 3000}).encode())
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    signature = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    resp = client.post(info["token_uri"], data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{header}.{claims}.{_b64(signature)}"})
    resp.raise_for_status()
    return resp.json()["access_token"]


def _write_tab(client: httpx.Client, token: str, conn: sqlite3.Connection, sheet_id: str, tab: str, rows: list[list]) -> str:
    content_hash = hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()
    state = conn.execute("SELECT * FROM export_state WHERE tab=?", (tab,)).fetchone()
    if state and state["content_hash"] == content_hash:
        return "unchanged"
    # Pad with blanks to cover rows from the last export. No clear-then-write, so a failed write can't leave an empty sheet.
    width = len(rows[0])
    previous = state["row_count"] if state else 0
    padded = rows + [[""] * width] * max(0, previous - len(rows))
    range_ = f"'{tab}'!A1"
    resp = client.put(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_}", params={"valueInputOption": "RAW"},
                      headers={"Authorization": f"Bearer {token}"}, json={"range": range_, "majorDimension": "ROWS", "values": padded})
    resp.raise_for_status()
    conn.execute("INSERT INTO export_state(tab,row_count,content_hash,at) VALUES(?,?,?,?) ON CONFLICT(tab) DO UPDATE SET row_count=excluded.row_count,content_hash=excluded.content_hash,at=excluded.at",
                 (tab, len(rows), content_hash, iso(utcnow())))
    return f"wrote {len(rows) - 1} rows"


def run(conn: sqlite3.Connection, s, transport: httpx.BaseTransport | None = None) -> dict:
    """A failed export is reported and never touches the CRM. RAW input stops client text from running as a formula."""
    if not (s.gig_sheet_id and s.google_sa_json):
        return {"skipped": True, "missing": [n for n, v in [("GIG_SHEET_ID", s.gig_sheet_id), ("GOOGLE_SERVICE_ACCOUNT_JSON", s.google_sa_json)] if not v]}
    try:
        info = json.loads(s.google_sa_json)
        with httpx.Client(timeout=30, transport=transport) as client:
            token = service_account_token(info, client)
            return {"skipped": False, GIG_TAB: _write_tab(client, token, conn, s.gig_sheet_id, GIG_TAB, gig_rows(conn)),
                    INQ_TAB: _write_tab(client, token, conn, s.gig_sheet_id, INQ_TAB, inquiry_rows(conn))}
    except Exception as exc:
        return {"skipped": False, "error": type(exc).__name__}
