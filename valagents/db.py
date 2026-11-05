"""The CRM's system of record. Plain SQLite on a Railway volume; every query is parameterized."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, org TEXT, role TEXT,
  source TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS contacts_phone ON contacts(phone) WHERE phone IS NOT NULL AND phone <> '';
CREATE TABLE IF NOT EXISTS bookings(
  id INTEGER PRIMARY KEY, contact_id INTEGER REFERENCES contacts(id), org TEXT, event_type TEXT,
  start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, venue TEXT, address TEXT, headcount INTEGER,
  package TEXT, campus TEXT, miles INTEGER, quote_cents INTEGER, deposit_cents INTEGER,
  stage TEXT NOT NULL, hold_expires_utc TEXT, quoted_at TEXT, deposit_at TEXT, notes TEXT, source TEXT,
  crm_external_id TEXT, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bookings_stage ON bookings(stage);
CREATE TABLE IF NOT EXISTS links(
  token_id TEXT PRIMARY KEY, booking_id INTEGER, action TEXT NOT NULL, expires_utc TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, to_addr TEXT, subject TEXT, body TEXT NOT NULL,
  booking_id INTEGER, kind TEXT NOT NULL, status TEXT NOT NULL, dedupe_key TEXT UNIQUE,
  in_reply_to TEXT, created_at TEXT NOT NULL, decided_at TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL, filename TEXT, sha256 TEXT, extracted TEXT,
  conflicts TEXT, method TEXT, created_at TEXT NOT NULL, UNIQUE(booking_id, sha256));
CREATE TABLE IF NOT EXISTS inquiries(
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, status TEXT NOT NULL, from_name TEXT, from_email TEXT, from_phone TEXT,
  org TEXT, role TEXT, event_type TEXT, event_date TEXT, start_time TEXT, end_time TEXT, venue TEXT, campus TEXT, headcount INTEGER,
  missing TEXT NOT NULL DEFAULT '[]', notes TEXT, clash_status TEXT NOT NULL DEFAULT 'not_checked', subject TEXT, excerpt TEXT,
  method TEXT, booking_id INTEGER, external_ref TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS email_seen(
  message_id_hash TEXT PRIMARY KEY, result TEXT NOT NULL, inquiry_id INTEGER, message_id TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls(
  call_sid TEXT PRIMARY KEY, from_phone TEXT, status TEXT, duration INTEGER, outcome TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_state(
  tab TEXT PRIMARY KEY, row_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, booking_id INTEGER, detail TEXT);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def audit(conn: sqlite3.Connection, actor: str, action: str, booking_id: int | None = None, detail: object = None) -> None:
    conn.execute(
        "INSERT INTO audit(at,actor,action,booking_id,detail) VALUES(?,?,?,?,?)",
        (iso(utcnow()), actor, action, booking_id, json.dumps(detail) if detail is not None else None),
    )
