"""Name Drops and live shoutouts: the wow layer. People's attention snaps to their own name, so the crowd
hears it in Volt's voice. Guests pick from fixed templates; they can't type free text, and Val approves each one."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import httpx

from .db import iso, parse, utcnow

OCCASIONS = {
    "birthday": "Happy birthday, {name}. This one's for you.",
    "grad": "{name} just graduated. Make some noise.",
    "crew": "{name} and the whole crew are in the building.",
    "first_time": "{name}, welcome to your first DJ Val party.",
}
DROP_TEMPLATES = {
    "intro": "{name}. You're locked in with DJ Val.",
    "hype": "{name}, this is your night. DJ Val on the decks.",
    "tag": "DJ Val. For {name}.",
}
# Slurs are caught even when spaced out with dots. Short everyday words only match as whole words, so Killian and
# Yoshito still work. This is a first filter: Val approves every name before it plays.
BLOCKED_ANYWHERE = {"nigger", "nigga", "faggot", "retard", "tranny", "bitch", "cunt", "fuck", "whore", "hitler"}
BLOCKED_WORDS = {"fag", "kike", "spic", "chink", "shit", "slut", "nazi", "rape", "kill", "die"}
NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '.\-][^\W\d_]+){0,4}\.?$")
RATE_SECONDS = 300


class ShoutoutError(ValueError):
    pass


def clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw or "").strip()
    if not name or len(name) > 30 or not NAME_RE.match(name):
        raise ShoutoutError("Just a first name or nickname, letters only.")
    squashed = re.sub(r"[^a-z]", "", name.lower())
    words = set(re.split(r"[ '.\-]+", name.lower()))
    if any(bad in squashed for bad in BLOCKED_ANYWHERE) or words & BLOCKED_WORDS:
        raise ShoutoutError("Try a different name.")
    return name


def drop_script(name: str, kind: str = "intro", say_as: str | None = None) -> str:
    if kind not in DROP_TEMPLATES:
        raise ValueError("Unknown drop type")
    return DROP_TEMPLATES[kind].format(name=say_as or name)


def chapter_drop_name(row) -> str:
    return row["chapter"] + " chapter" if row["chapter"] else row["org"]


def request_shoutout(conn: sqlite3.Connection, code: str, raw_name: str, occasion: str, ip: str, honeypot: str, salt: str) -> int | None:
    event = conn.execute("SELECT * FROM shout_events WHERE code=? AND active=1", (code,)).fetchone()
    if event is None:
        raise ShoutoutError("Shoutouts aren't open for this event.")
    if honeypot:
        return None  # spam trap: look successful, store nothing
    if occasion not in OCCASIONS:
        raise ShoutoutError("Pick one of the options.")
    name = clean_name(raw_name)
    ip_hash = hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()
    last = conn.execute("SELECT created_at FROM shoutouts WHERE event_code=? AND ip_hash=? ORDER BY id DESC LIMIT 1", (code, ip_hash)).fetchone()
    if last:
        if (utcnow() - parse(last["created_at"])).total_seconds() < RATE_SECONDS:
            raise ShoutoutError("One at a time. Try again in a few minutes.")
    cur = conn.execute("INSERT INTO shoutouts(event_code,name,occasion,script,status,ip_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                       (code, name, occasion, OCCASIONS[occasion].format(name=name), "pending", ip_hash, iso(utcnow())))
    return cur.lastrowid


def decide_shoutout(conn: sqlite3.Connection, shout_id: int, approved: bool) -> sqlite3.Row:
    cur = conn.execute("UPDATE shoutouts SET status=? WHERE id=? AND status='pending'", ("approved" if approved else "rejected", shout_id))
    if cur.rowcount != 1:
        raise ShoutoutError("Already decided.")
    return conn.execute("SELECT * FROM shoutouts WHERE id=?", (shout_id,)).fetchone()


def synthesize(script: str, key: str, voice_id: str, out: Path, transport: httpx.BaseTransport | None = None) -> Path:
    """Text to speech with the house voice. Requires a paid ElevenLabs plan for commercial use."""
    if not key or not voice_id:
        raise ValueError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are needed to make audio.")
    with httpx.Client(timeout=60, transport=transport) as client:
        resp = client.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", headers={"xi-api-key": key},
                           json={"text": script, "model_id": "eleven_multilingual_v2"})
        resp.raise_for_status()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return out
