"""Tools Blayke runs by hand: Name Drop scripts for every chapter, a set draft from Val's library, a Night Card."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import crates, drops, wrapped
from .settings import ROOT, load


def _drops(a) -> int:
    rows = json.loads((ROOT / "data" / "chapters.json").read_text())
    out = Path(a.out)
    s = load()
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "org", "campus", "instagram", "kind", "script", "audio_file", "note"])
        for r in rows:
            name = drops.chapter_drop_name(r)
            script = drops.drop_script(name, a.kind)
            audio = ""
            if a.make_audio:
                audio = str(drops.synthesize(script, s.elevenlabs_key, s.elevenlabs_voice, out.parent / "drops" / f"{r['id']}.mp3"))
            w.writerow([r["id"], r["org"], r["campus"], r["ig"], a.kind, script, audio,
                        "Sample for this chapter only. Public use needs their OK. Check pronunciation."])
    print(f"Wrote {len(rows)} scripts to {out}")
    return 0


def _crate(a) -> int:
    blob = Path(a.library).read_bytes()
    try:
        draft = crates.build(crates.parse_library(blob), a.minutes, a.must, a.dnp, a.tag)
    except crates.CrateError as exc:
        print(exc, file=sys.stderr)
        return 1
    Path(a.out).write_bytes(crates.to_rekordbox_xml(blob, draft, a.name))
    print(f"{len(draft['tracks'])} tracks in '{a.name}'. Requests not in the library: {draft['unmatched_requests'] or 'none'}")
    return 0


def _card(a) -> int:
    tracks = wrapped.read_history(Path(a.history).read_text(errors="replace")) if a.history else []
    moments = len(json.loads(Path(a.moments).read_text()).get("moments", [])) if a.moments else None
    items = wrapped.stats(tracks, a.minutes, a.peak, moments)
    print(wrapped.render(a.host, a.date, items, Path(a.out)))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="valagents")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drops"); d.add_argument("--out", required=True); d.add_argument("--kind", default="intro"); d.add_argument("--make-audio", action="store_true"); d.set_defaults(fn=_drops)
    c = sub.add_parser("crate"); c.add_argument("library"); c.add_argument("--minutes", type=int, required=True); c.add_argument("--name", required=True)
    c.add_argument("--must", action="append", default=[]); c.add_argument("--dnp", action="append", default=[]); c.add_argument("--tag", action="append", default=[]); c.add_argument("--out", required=True); c.set_defaults(fn=_crate)
    w = sub.add_parser("card"); w.add_argument("--host", required=True); w.add_argument("--date", required=True); w.add_argument("--history"); w.add_argument("--minutes", type=int)
    w.add_argument("--peak"); w.add_argument("--moments"); w.add_argument("--out", required=True); w.set_defaults(fn=_card)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
