"""Is Val free? Checks the CRM's own holds and bookings, plus his private calendar (iCal) feed."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from .db import iso, parse, utcnow
from .settings import TZ

BLOCKING = ("held", "quoted", "confirmed")


class BadTime(ValueError):
    pass


def local_window(day: str, start: str, end: str) -> tuple[datetime, datetime]:
    """'2026-11-14', '22:00', '02:00' -> aware start and end. An end at or before the start is next day."""
    try:
        d = date.fromisoformat(day)
        s = time.fromisoformat(start)
        e = time.fromisoformat(end)
    except ValueError as exc:
        raise BadTime("Use date YYYY-MM-DD and times HH:MM") from exc
    begin = datetime.combine(d, s, TZ)
    finish = datetime.combine(d if e > s else d + timedelta(days=1), e, TZ)
    if (finish - begin) > timedelta(hours=12):
        raise BadTime("Sets longer than 12 hours need Val directly")
    return begin, finish


def parse_ics(text: str) -> list[tuple[datetime, datetime, str]]:
    """Reads busy blocks from an .ics feed. Known limit: a recurring event only blocks its first date
    (RRULEs aren't expanded), and Val still re-checks every date when he approves it. See README."""
    unfolded = text.replace("\r\n", "\n").replace("\n ", "").replace("\n\t", "")
    events, cur = [], None
    for line in unfolded.split("\n"):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            start, end = cur.get("DTSTART"), cur.get("DTEND")
            if start:
                s = _ics_dt(start, cur.get("DTSTART_TZ"))
                e = _ics_dt(end, cur.get("DTEND_TZ")) if end else s + (timedelta(days=1) if len(start) == 8 else timedelta(hours=1))
                if cur.get("STATUS") != "CANCELLED" and cur.get("TRANSP") != "TRANSPARENT":
                    events.append((s, e, cur.get("SUMMARY", "Busy")))
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            key, *params = head.split(";")
            cur[key] = value.strip()
            for p in params:
                if p.startswith("TZID="):
                    cur[f"{key}_TZ"] = p[5:]
    return events


def _ics_dt(value: str, tzid: str | None) -> datetime:
    from zoneinfo import ZoneInfo

    if len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=TZ)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    zone = TZ
    if tzid:
        try:
            zone = ZoneInfo(tzid)
        except Exception:
            zone = TZ
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone)


def conflicts(conn: sqlite3.Connection, cfg: dict, start: datetime, end: datetime,
              exclude: int | None = None, busy: list | None = None) -> list[str]:
    gap = timedelta(minutes=int(cfg["gap_minutes_between_gigs"]))
    a, b = start - gap, end + gap
    found = []
    now = iso(utcnow())
    rows = conn.execute(
        "SELECT id,org,start_utc,end_utc,stage,hold_expires_utc FROM bookings WHERE stage IN (?,?,?)", BLOCKING
    ).fetchall()
    for r in rows:
        if r["id"] == exclude:
            continue
        if r["stage"] == "held" and r["hold_expires_utc"] and r["hold_expires_utc"] <= now:
            continue
        if parse(r["start_utc"]) < b and parse(r["end_utc"]) > a:
            found.append(f"booking #{r['id']}")
    for s, e, name in busy or []:
        if s < b and e > a:
            found.append(f"calendar: {name}")
    return found
