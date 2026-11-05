"""Demand Desk: ranks the 53 chapters and writes today's DMs. Humans send them. Instagram bans automated cold DMs.
Only claim allowed about Val: what's true in the config. Unverified handles are skipped until someone finds them."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

CLAIM = "I play for the Ques at IU"
SKIP = {"Verify active", "Not interested", "Current client", "Booked", "Replied", "Messaged"}


def seed(conn: sqlite3.Connection, path: str | Path) -> int:
    rows = json.loads(Path(path).read_text())
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO chapters(id,org,chapter,campus,council,ig,drive,status,notes,sort) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["org"], r["chapter"], r["campus"], r["council"], r["ig"], r["drive"], r["status"], r["notes"], r["order"]),
        )
    return len(rows)


def days_to_founders(org: str, cfg: dict, today: date) -> int | None:
    mmdd = cfg["founders_days"].get(org)
    if not mmdd:
        return None
    m, d = map(int, mmdd.split("-"))
    this = date(today.year, m, d)
    if this < today:
        this = date(today.year + 1, m, d)
    return (this - today).days


def score(r, cfg: dict, today: date) -> tuple:
    days = days_to_founders(r["org"], cfg, today)
    in_window = days is not None and 10 <= days <= 35
    return (0 if in_window else 1, days if in_window else 999, 0 if r["ig"] else 1,
            0 if r["org"] == "Omega Psi Phi" else 1, 0 if r["campus"] == "Purdue" else 1, r["sort"])


def write_dm(r, cfg: dict, today: date, has_drop: bool) -> str:
    who = f"{r['chapter']} chapter" if r["chapter"] else r["org"]
    days = days_to_founders(r["org"], cfg, today)
    drop = " I made a 6-second intro drop with your chapter's name, it's attached." if has_drop else ""
    if days is not None and 10 <= days <= 35:
        return f"Hey {who}, I'm DJ Val. {CLAIM}. Booking the weekends around your Founders' Day now.{drop} Want a 15-second clip and my rates?"
    return f"Hey {who}, I'm DJ Val. {CLAIM}. Locking in spring formals and after-parties now.{drop} Can I send a clip and my rates?"


def todays_list(conn: sqlite3.Connection, cfg: dict, today: date, limit: int = 10, drops: set[str] | None = None) -> list[dict]:
    drops = drops or set()
    rows = [r for r in conn.execute("SELECT * FROM chapters").fetchall() if r["status"] not in SKIP and r["ig"]]
    rows.sort(key=lambda r: score(r, cfg, today))
    return [{"id": r["id"], "org": r["org"], "chapter": r["chapter"], "campus": r["campus"], "ig": r["ig"],
             "dm": write_dm(r, cfg, today, r["id"] in drops)} for r in rows[:limit]]


def mark(conn: sqlite3.Connection, chapter_id: str, status: str, today: date) -> None:
    if status not in {"Messaged", "Replied", "Booked", "Not interested"}:
        raise ValueError("Unknown status")
    cur = conn.execute("UPDATE chapters SET status=?,last_contact=? WHERE id=?", (status, today.isoformat(), chapter_id))
    if cur.rowcount != 1:
        raise ValueError("Unknown chapter")
