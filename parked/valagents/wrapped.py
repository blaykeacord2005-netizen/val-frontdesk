"""Night Card: a shareable 9:16 card for the host the morning after. Real numbers only: anything we
don't have is left off, never estimated. Reads a Rekordbox history export and Drop Finder's moments.json."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG, FG, DIM, ACCENT = (10, 10, 16), (245, 245, 250), (150, 150, 170), (199, 255, 62)
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"]


def _font(size: int, bold: bool = True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def read_history(csv_text: str) -> list[dict]:
    """Rekordbox history exports are tab- or comma-separated with Track Title and Artist columns."""
    sample = csv_text[:2000]
    delim = "\t" if "\t" in sample else ","
    rows = list(csv.DictReader(io.StringIO(csv_text), delimiter=delim))
    tracks = []
    for r in rows:
        low = {(k or "").strip().lower().lstrip("﻿"): (v or "").strip() for k, v in r.items()}
        title = low.get("track title") or low.get("title") or low.get("name")
        if title:
            tracks.append({"title": title, "artist": low.get("artist", "")})
    return tracks


def stats(tracks: list[dict], set_minutes: int | None, peak_label: str | None, moments: int | None) -> list[tuple[str, str]]:
    out = []
    if tracks:
        out.append((str(len(tracks)), "tracks played"))
    if set_minutes:
        hours = set_minutes / 60
        out.append((f"{hours:g}", "hours on the decks"))
    if moments:
        out.append((str(moments), "crowd peaks caught"))
    if peak_label:
        out.append((peak_label, "the moment it peaked"))
    return out


def _wrap(draw, text, font, width):
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def render(host: str, date_label: str, items: list[tuple[str, str]], out: Path) -> Path:
    if not host.strip():
        raise ValueError("Need the host's name")
    if not items:
        raise ValueError("No real numbers to show, so no card was made")
    img = Image.new("RGB", (1080, 1920), BG)
    d = ImageDraw.Draw(img)
    x, y, width = 90, 190, 900
    d.text((x, y), "THE NIGHT IN NUMBERS", font=_font(40), fill=ACCENT)
    y += 90
    title_font = _font(92)
    for line in _wrap(d, host, title_font, width)[:3]:
        d.text((x, y), line, font=title_font, fill=FG)
        y += 108
    d.text((x, y + 8), date_label, font=_font(38, False), fill=DIM)
    y += 130
    for big, small in items[:4]:
        big_font = _font(132 if len(big) <= 6 else 88)
        d.text((x, y), big, font=big_font, fill=FG)
        y += big_font.size + 22
        d.text((x, y), small, font=_font(38, False), fill=DIM)
        y += 96
    d.rectangle((x, 1660, x + 120, 1668), fill=ACCENT)
    d.text((x, 1690), "DJ VAL", font=_font(58), fill=FG)
    d.text((x, 1766), "@_valentintrujillo", font=_font(36, False), fill=DIM)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
