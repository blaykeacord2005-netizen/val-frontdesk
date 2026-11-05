"""Crate Builder: a Rekordbox XML library plus event details gives a first-draft set Val can import and edit.
Uses only tracks he already owns. It drafts order and energy; the craft of the set stays his."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

MAX_XML_BYTES = 200 * 1024 * 1024
ARC = [("warm", 0.25, (1, 4)), ("build", 0.35, (4, 7)), ("peak", 0.30, (7, 10)), ("close", 0.10, (4, 7))]


class CrateError(ValueError):
    pass


@dataclass
class Track:
    id: str
    name: str
    artist: str
    bpm: float
    key: str
    genre: str
    comments: str
    seconds: int
    energy: int


def _energy(comments: str, bpm: float) -> int:
    m = re.search(r"energy\s*[:=]?\s*(\d{1,2})", comments, re.I) or re.match(r"\s*(\d{1,2})\s*[-–]", comments)
    if m and 1 <= int(m[1]) <= 10:
        return int(m[1])
    # No Mixed In Key tag: rough estimate from tempo, 90 BPM ~ 3, 130 BPM ~ 8.
    return max(1, min(10, round((bpm - 66) / 8))) if bpm else 5


def parse_library(xml_bytes: bytes) -> list[Track]:
    if len(xml_bytes) > MAX_XML_BYTES:
        raise CrateError("Library file is too big.")
    if b"<!DOCTYPE" in xml_bytes[:2000] or b"<!ENTITY" in xml_bytes[:2000]:
        raise CrateError("That file has unsafe XML in it.")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise CrateError("That isn't a readable Rekordbox XML file.") from exc
    collection = root.find("COLLECTION")
    if collection is None:
        raise CrateError("No COLLECTION found. Export from Rekordbox: File > Export Collection in xml format.")
    tracks = []
    for t in collection.findall("TRACK"):
        tid = t.get("TrackID")
        if not tid:
            continue
        bpm = float(t.get("AverageBpm") or 0)
        comments = t.get("Comments") or ""
        tracks.append(Track(tid, t.get("Name") or "", t.get("Artist") or "", bpm, t.get("Tonality") or "", t.get("Genre") or "",
                            comments, int(t.get("TotalTime") or 0), _energy(comments, bpm)))
    return tracks


def _matches(track: Track, wanted: str) -> bool:
    w = wanted.strip().lower()
    return bool(w) and (w in track.name.lower() or w in track.artist.lower())


def build(tracks: list[Track], set_minutes: int, must_play: list[str] | None = None, do_not_play: list[str] | None = None,
          tags: list[str] | None = None, slot_seconds: int = 210) -> dict:
    """slot_seconds: a working DJ mixes out early, so count plays per slot rather than full track lengths."""
    if not tracks:
        raise CrateError("The library has no tracks.")
    if set_minutes <= 0:
        raise CrateError("Set length must be positive.")
    banned = [b for b in (do_not_play or []) if b.strip()]
    pool = [t for t in tracks if not any(_matches(t, b) for b in banned)]
    tagged = [t for t in pool if any(g.lower() in (t.genre + " " + t.comments).lower() for g in (tags or []))]
    base = tagged if len(tagged) * slot_seconds >= set_minutes * 60 else pool
    target = max(1, set_minutes * 60 // slot_seconds)
    chosen: list[tuple[str, Track]] = []
    used: set[str] = set()
    unmatched = []
    forced = []
    for wanted in must_play or []:
        hit = next((t for t in pool if _matches(t, wanted) and t.id not in used), None)
        if hit:
            used.add(hit.id)
            forced.append(hit)
        elif wanted.strip():
            unmatched.append(wanted.strip())
    for section, share, (low, high) in ARC:
        need = round(target * share)
        fits = [t for t in forced if low <= t.energy < high + (section == "peak")]
        for t in fits:
            forced.remove(t)
            chosen.append((section, t))
            need -= 1
        options = [t for t in base if low <= t.energy <= high and t.id not in used]
        options.sort(key=lambda t: (t.bpm, t.energy), reverse=section == "close")
        for t in options[:max(0, need)]:
            used.add(t.id)
            chosen.append((section, t))
    chosen.extend(("build", t) for t in forced)  # must-plays that matched no section still make the set
    order = {name: i for i, (name, _, _) in enumerate(ARC)}
    chosen.sort(key=lambda p: order[p[0]])  # stable: keeps each section's tempo ramp
    return {"tracks": [{"section": sec, "id": t.id, "name": t.name, "artist": t.artist, "bpm": t.bpm, "key": t.key, "energy": t.energy}
                       for sec, t in chosen],
            "target_count": target, "unmatched_requests": unmatched, "used_tag_filter": base is tagged}


def to_rekordbox_xml(library_xml: bytes, draft: dict, playlist_name: str) -> bytes:
    """Returns the library with a new playlist added, ready to import back into Rekordbox."""
    root = ET.fromstring(library_xml)
    playlists = root.find("PLAYLISTS")
    if playlists is None:
        playlists = ET.SubElement(root, "PLAYLISTS")
        ET.SubElement(playlists, "NODE", {"Type": "0", "Name": "ROOT", "Count": "0"})
    top = playlists.find("NODE")
    if top is None:
        top = ET.SubElement(playlists, "NODE", {"Type": "0", "Name": "ROOT", "Count": "0"})
    node = ET.SubElement(top, "NODE", {"Name": playlist_name, "Type": "1", "KeyType": "0", "Entries": str(len(draft["tracks"]))})
    for t in draft["tracks"]:
        ET.SubElement(node, "TRACK", {"Key": t["id"]})
    top.set("Count", str(len(top.findall("NODE"))))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
