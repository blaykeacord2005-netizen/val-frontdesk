"""One-page gig sheet and gear list. Tackles Val's setup time: no surprises at the venue."""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

from .crm import local


def gear_list(cfg: dict, d: dict, package: str) -> list[str]:
    gear = list(cfg["gear_base"])
    if d.get("house_sound") is not True:
        gear.append("Speakers and stands (no confirmed house system)")
    else:
        gear.append("Mixer-to-house cables (XLR and RCA)")
    if d.get("table_provided") is not True:
        gear.append("Folding table (no confirmed table)")
    if d.get("power_within_25ft") is not True:
        gear.append("50-ft extension cord (power not confirmed within 25 ft)")
    if package in ("chapter_night", "recap"):
        gear.append("Lights")
    if d.get("indoors") is False:
        gear.append("Canopy and rain covers (outdoor event)")
    return gear


def load_in_time(start: datetime, details: dict, setup_minutes: int) -> str:
    if details.get("load_in"):
        h, m = map(int, details["load_in"].split(":"))
        return start.replace(hour=h, minute=m).strftime("%-I:%M %p")
    return (start - timedelta(minutes=setup_minutes)).strftime("%-I:%M %p") + " (estimate)"


def render(b, contact, cfg: dict) -> str:
    d = json.loads(b["details"] or "{}")
    e = lambda v: html.escape(str(v)) if v not in (None, "") else "<em>not confirmed</em>"
    yn = lambda v: "Yes" if v is True else "No" if v is False else "<em>not confirmed</em>"
    s, t = local(b["start_utc"]), local(b["end_utc"])
    rows = [
        ("Date", f"{s.strftime('%A, %B')} {s.day}, {s.year}"), ("Set", f"{s.strftime('%-I:%M %p')} to {t.strftime('%-I:%M %p')}"),
        ("Load in", html.escape(load_in_time(s, d, int(cfg["setup_minutes"])))), ("Org", e(b["org"])),
        ("Venue", e(d.get("venue") or b["venue"])), ("Address", e(d.get("address"))),
        ("Day-of contact", f"{e(d.get('day_of_contact'))} · {e(d.get('day_of_phone'))}"),
        ("Booked by", f"{e(contact['name'])} · {e(contact['phone'] or contact['email'])}"),
        ("Headcount", e(d.get("headcount") or b["headcount"])), ("Table", yn(d.get("table_provided"))),
        ("Power within 25 ft", yn(d.get("power_within_25ft"))), ("House sound", yn(d.get("house_sound"))),
        ("Insurance certificate", yn(d.get("coi_required")) + (f" · name {e(d['additional_insured'])}" if d.get("additional_insured") else "")),
        ("Must play", e(d.get("must_play"))), ("Do not play", e(d.get("do_not_play"))), ("Stroll segment", yn(d.get("stroll_segment"))),
        ("Name in intro drop OK", yn(d.get("chapter_name_ok"))), ("Filming OK", yn(d.get("filming_ok"))),
    ]
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    gear = "".join(f"<li>{html.escape(g)}</li>" for g in gear_list(cfg, d, b["package"]))
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Gig sheet #{b['id']}</title><style>body{{font:16px system-ui;max-width:640px;margin:auto;padding:16px;background:#0b0b10;color:#f4f4f6}}"
            "th{text-align:left;color:#a7a7b5;font-weight:500;padding:6px 12px 6px 0;vertical-align:top}td{padding:6px 0}em{color:#ffb020;font-style:normal}"
            "li{padding:3px 0}h1{font-size:22px}</style>"
            f"<h1>Gig sheet: {e(b['org'])}</h1><table>{table}</table><h2>Pack list</h2><ul>{gear}</ul>")
