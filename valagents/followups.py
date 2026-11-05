"""Daily run. Expires holds, drafts follow-ups, closes out played gigs, drafts rebooking.
Every client-facing item is a draft with a dedupe key, so a re-run can't double up."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from .crm import contact_of, local, pretty_when
from .db import audit, iso, parse
from .messaging import alert, draft
from .settings import TZ, Settings


def _to(c):
    return ("email", c["email"]) if c["email"] else ("text", c["phone"])


def _next_date(mmdd: str, today: date) -> date:
    month, day = map(int, mmdd.split("-"))
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


def run_daily(conn: sqlite3.Connection, s: Settings, cfg: dict, now: datetime) -> dict:
    counts = {"expired": 0, "followups": 0, "reminders": 0, "played": 0, "rebook": 0}
    today = now.astimezone(TZ).date()

    for b in conn.execute("SELECT * FROM bookings WHERE stage='held' AND hold_expires_utc<=?", (iso(now),)).fetchall():
        conn.execute("UPDATE bookings SET stage='expired',updated_at=? WHERE id=?", (iso(now), b["id"]))
        audit(conn, "daily", "booking.expired", b["id"])
        alert(conn, s, s.alert_val, f"Hold expired: {b['org']}", f"{pretty_when(b)} is open again. Nobody quoted it in time.", b["id"], f"expired-{b['id']}")
        counts["expired"] += 1

    for b in conn.execute("SELECT * FROM bookings WHERE stage='quoted'").fetchall():
        if not b["quoted_at"] or parse(b["start_utc"]) <= now:
            continue
        age = (now - parse(b["quoted_at"])).days
        c = contact_of(conn, b)
        channel, to = _to(c)
        first = c["name"].split()[0]
        if age >= 7:
            made = draft(conn, channel, to, "DJ Val: last check", f"Hey {first}, last one from me. If {pretty_when(b)} still matters, a deposit locks it. No worries if plans changed.",
                         b["id"], "followup", f"fu7-{b['id']}")
        elif age >= 3:
            made = draft(conn, channel, to, "DJ Val: your date", f"Hey {first}, still holding space for {pretty_when(b)}? Happy to answer anything about the quote.",
                         b["id"], "followup", f"fu3-{b['id']}")
        else:
            made = None
        counts["followups"] += 1 if made else 0

    for b in conn.execute("SELECT * FROM bookings WHERE stage='confirmed'").fetchall():
        start, end = parse(b["start_utc"]), parse(b["end_utc"])
        c = contact_of(conn, b)
        channel, to = _to(c)
        if end <= now:
            conn.execute("UPDATE bookings SET stage='played',updated_at=? WHERE id=?", (iso(now), b["id"]))
            audit(conn, "daily", "booking.played", b["id"])
            counts["played"] += 1
            draft(conn, channel, to, "Thanks from DJ Val",
                  f"Thanks for having me, {c['name'].split()[0]}. Could you send one sentence on how the night went? With your OK, I'd love to share it.",
                  b["id"], "testimonial", f"thanks-{b['id']}")
            if b["package"] == "recap" and s.alert_max:
                alert(conn, s, s.alert_max, f"Recap due in 48 hours: {b['org']}", "Upload the footage so Drop Finder can cut it.", b["id"], f"recap-{b['id']}")
            continue
        days = (local(b["start_utc"]).date() - today).days
        if days == 3:
            counts["reminders"] += 1 if draft(conn, channel, to, "DJ Val: balance", "Quick reminder: the balance is due the day before the event. See you soon.",
                                              b["id"], "balance", f"bal-{b['id']}") else 0
        if days == 2:
            alert(conn, s, s.alert_val, f"Two days out: {b['org']}", f"{pretty_when(b)}. Gig sheet: {s.public_url}/admin/gigsheet/{b['id']}", b["id"], f"prep-{b['id']}")

    # Rebooking: officers turn over and Founders' Days repeat, so past clients get drafts at those moments.
    latest = conn.execute(
        "SELECT b.* FROM bookings b JOIN (SELECT org, MAX(end_utc) AS last_end FROM bookings WHERE stage='played' GROUP BY org) x "
        "ON b.org=x.org AND b.end_utc=x.last_end WHERE b.stage='played'").fetchall()
    for b in latest:
        c = contact_of(conn, b)
        channel, to = _to(c)
        for mmdd in cfg["officer_turnover_dates"]:
            if _next_date(mmdd, today) == today:
                counts["rebook"] += 1 if draft(conn, channel, to, "DJ Val: new semester",
                                               f"Hey, it's Val. New semester, and I know officers change. Who's handling {b['org']}'s events now? I'd love to play for you again.",
                                               b["id"], "rebook", f"turnover-{b['org']}-{today.year}-{mmdd}") else 0
        for org, mmdd in cfg["founders_days"].items():
            if org.lower() in (b["org"] or "").lower():
                fd = _next_date(mmdd, today)
                if (fd - today).days == 28:
                    counts["rebook"] += 1 if draft(conn, channel, to, "DJ Val: Founders' Day",
                                                   f"Hey, it's Val. Founders' Day is coming up on {fd.strftime('%B')} {fd.day}. If {b['org']} is planning a party around it, I'd love to hold a date for you.",
                                                   b["id"], "rebook", f"fd-{b['org']}-{fd.year}") else 0
    return counts


def report(conn: sqlite3.Connection) -> dict:
    stages = {r["stage"]: r["n"] for r in conn.execute("SELECT stage,COUNT(*) n FROM bookings GROUP BY stage")}
    money = conn.execute("SELECT COALESCE(SUM(quote_cents),0) c FROM bookings WHERE stage IN ('confirmed','played')").fetchone()["c"]
    open_inq = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM inquiries GROUP BY status")}
    waiting = conn.execute("SELECT COUNT(*) n FROM outbox WHERE status='draft'").fetchone()["n"]
    return {"pipeline": stages, "booked_cents": money, "inquiries": open_inq, "drafts_waiting": waiting}
