import io
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from conftest import AUTH, SECRET, hold, links_in_alert, next_saturday
from valagents import crates, crm, demand, drops, gigsheet, onboarding, wrapped
from valagents.checkcherry import CheckCherry

CFG = json.load(open("data/val_config.json"))
PAPERWORK = b"""Venue: The Barn
Address: 1720 W That Rd
Load-in: 7:30 pm
Event contact: Jay Smith
Contact phone: 765-555-0199
Expected headcount: 200
Table provided: no
Power within 25 feet: yes
House sound: no
COI required: yes
Additional insured: Rho Sigma Chapter
Do-not-play: Mr. Brightside
Filming OK: yes
Chapter name OK: yes
"""


def confirmed(client, conn, **kw):
    r = hold(client, **kw)
    approve, _ = links_in_alert(conn, r["booking_id"])
    client.post(f"/a/{approve}")
    res = client.post(f"/admin/bookings/{r['booking_id']}/deposit", auth=AUTH)
    assert res.status_code == 200
    return r["booking_id"]


def intake_token(conn):
    body = conn.execute("SELECT body FROM outbox WHERE kind='intake'").fetchone()["body"]
    import re
    return re.search(r"/onboard/([\w\-.]+)", body)[1]


# ---- demand desk ----
@pytest.fixture
def seeded(conn):
    assert demand.seed(conn, "data/chapters.json") == 53
    return conn


def test_seed_is_idempotent_and_counts_match(seeded):
    demand.seed(seeded, "data/chapters.json")
    counts = {r["status"]: r["n"] for r in seeded.execute("SELECT status,COUNT(*) n FROM chapters GROUP BY status")}
    assert counts == {"Not contacted": 46, "Current client": 1, "Verify active": 6}


def test_list_skips_no_handle_and_ranks_founders_window(seeded):
    items = demand.todays_list(seeded, CFG, date(2026, 11, 5))
    assert items and all(i["ig"] for i in items)
    assert len(items) <= 10
    # Nov 5: Alpha Dec 4 is 29 days out and Ques Nov 17 is 12. The Ques must lead.
    assert items[0]["ig"] == "@rhosig_ques"
    assert {"@gammarho_alphas", "@rhosig_ques"} <= {i["ig"] for i in items}


def test_dm_is_truthful_short_and_never_sent(seeded):
    for i in demand.todays_list(seeded, CFG, date(2026, 11, 5), drops={"c25"}):
        words = i["dm"].split()
        assert len(words) <= 45
        low = i["dm"].lower()
        assert "purdue" not in low and "ball state" not in low and "insured" not in low and "drink" not in low
    assert seeded.execute("SELECT COUNT(*) n FROM outbox").fetchone()["n"] == 0


def test_marking_sent_removes_from_list(seeded):
    demand.mark(seeded, "c25", "Messaged", date(2026, 11, 5))
    assert "@rhosig_ques" not in {i["ig"] for i in demand.todays_list(seeded, CFG, date(2026, 11, 5))}
    with pytest.raises(ValueError):
        demand.mark(seeded, "c25", "Hacked", date(2026, 11, 5))
    with pytest.raises(ValueError):
        demand.mark(seeded, "nope", "Messaged", date(2026, 11, 5))


def test_founders_dates_real_weekdays():
    assert demand.days_to_founders("Omega Psi Phi", CFG, date(2026, 11, 5)) == 12
    assert demand.days_to_founders("Iota Phi Theta", CFG, date(2026, 11, 5)) == (date(2027, 9, 19) - date(2026, 11, 5)).days


# ---- crates ----
def lib(n=120):
    rows = []
    for i in range(n):
        bpm = 80 + (i % 60)
        rows.append(f'<TRACK TrackID="{i}" Name="Song {i}" Artist="Artist {i % 10}" AverageBpm="{bpm}.00" TotalTime="200" Tonality="8A" '
                    f'Genre="{"Hip Hop" if i % 2 else "House"}" Comments="Energy {1 + i % 10}"/>')
    return f'<?xml version="1.0"?><DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="{n}">{"".join(rows)}</COLLECTION><PLAYLISTS><NODE Type="0" Name="ROOT" Count="0"/></PLAYLISTS></DJ_PLAYLISTS>'.encode()


def test_crate_size_order_and_no_invented_tracks():
    tracks = crates.parse_library(lib())
    d = crates.build(tracks, 240)
    assert d["target_count"] == 68
    assert abs(len(d["tracks"]) - 68) <= 2
    ids = {t.id for t in tracks}
    got = [t["id"] for t in d["tracks"]]
    assert set(got) <= ids and len(got) == len(set(got))
    assert [t["section"] for t in d["tracks"]] == sorted((t["section"] for t in d["tracks"]), key=["warm", "build", "peak", "close"].index)
    avg = lambda s: sum(t["energy"] for t in d["tracks"] if t["section"] == s) / max(1, sum(t["section"] == s for t in d["tracks"]))
    assert avg("warm") < avg("build") < avg("peak") and avg("close") < avg("peak")


def test_do_not_play_and_must_play_and_unmatched():
    tracks = crates.parse_library(lib())
    d = crates.build(tracks, 120, must_play=["Song 5", "Not In Library"], do_not_play=["Artist 3"])
    names = [t["name"] for t in d["tracks"]]
    assert "Song 5" in names and not any(t["artist"] == "Artist 3" for t in d["tracks"])
    assert d["unmatched_requests"] == ["Not In Library"]


def test_do_not_play_beats_must_play():
    d = crates.build(crates.parse_library(lib()), 60, must_play=["Song 3"], do_not_play=["Song 3"])
    assert "Song 3" not in [t["name"] for t in d["tracks"]] and d["unmatched_requests"] == ["Song 3"]


def test_tags_filter_only_when_enough_tracks():
    tracks = crates.parse_library(lib())
    d = crates.build(tracks, 60, tags=["Hip Hop"])
    assert d["used_tag_filter"] and all(int(t["id"]) % 2 for t in d["tracks"])
    assert crates.build(tracks, 60, tags=["Polka"])["used_tag_filter"] is False


def test_xml_roundtrip_and_bad_files():
    src = lib()
    d = crates.build(crates.parse_library(src), 60)
    out = crates.to_rekordbox_xml(src, d, "Rho Sigma & Friends")
    import xml.etree.ElementTree as ET
    node = ET.fromstring(out).find("PLAYLISTS/NODE/NODE")
    assert node.get("Name") == "Rho Sigma & Friends" and len(node.findall("TRACK")) == len(d["tracks"])
    with pytest.raises(crates.CrateError):
        crates.parse_library(b"<nope")
    with pytest.raises(crates.CrateError):
        crates.parse_library(b"<DJ_PLAYLISTS/>")
    with pytest.raises(crates.CrateError):
        crates.parse_library(b'<!DOCTYPE x [<!ENTITY a "b">]><DJ_PLAYLISTS/>')
    with pytest.raises(crates.CrateError):
        crates.build([], 60)


def test_energy_from_tempo_when_untagged():
    assert crates._energy("", 90) == 3 and crates._energy("", 130) == 8 and crates._energy("7 - 8A", 90) == 7 and crates._energy("Energy 2", 130) == 2


# ---- drops and shoutouts ----
def test_drop_scripts_use_only_the_name():
    s = drops.drop_script("Rho Sigma chapter")
    assert s.startswith("Rho Sigma chapter.") and "chant" not in s
    assert "Roe" in drops.drop_script("Rho", say_as="Roe")
    with pytest.raises(ValueError):
        drops.drop_script("x", "nope")


def event(conn, code="HAL", active=1):
    conn.execute("INSERT INTO shout_events(code,name,active) VALUES(?,?,?)", (code, "Test Night", active))


def test_shoutout_flow_needs_approval_and_rate_limit(client, conn):
    event(conn)
    r = client.post("/s/HAL", data={"name": "Maya", "occasion": "birthday"})
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM shoutouts").fetchone()
    assert row["status"] == "pending" and row["script"] == "Happy birthday, Maya. This one's for you."
    assert client.post("/s/HAL", data={"name": "Maya2", "occasion": "birthday"}).status_code == 400
    assert client.post(f"/admin/shouts/item/{row['id']}/approve").status_code == 401
    assert "Maya" in client.get("/admin/shouts/HAL", auth=AUTH).text
    assert client.post(f"/admin/shouts/item/{row['id']}/approve", auth=AUTH).json()["status"] == "approved"
    assert client.post(f"/admin/shouts/item/{row['id']}/approve", auth=AUTH).status_code == 409


@pytest.mark.parametrize("name", ["", "http://spam.com", "Call 7655550100", "<script>", "F.u.c.k", "a" * 31, "Bitch", "kill Maya"])
def test_bad_shoutout_names(name):
    with pytest.raises(drops.ShoutoutError):
        drops.clean_name(name)


@pytest.mark.parametrize("name", ["Maya", "José", "Mary-Kate", "O'Neil", "D.J.", "Anne Marie", "Killian", "Yoshito", "Dickson"])
def test_good_names_pass(name):
    assert drops.clean_name(name) == name


def test_shoutout_closed_event_bad_occasion_honeypot(client, conn):
    assert client.get("/s/NOPE").status_code == 404
    event(conn, "OFF", 0)
    assert client.post("/s/OFF", data={"name": "Maya", "occasion": "birthday"}).status_code == 400
    event(conn, "ON")
    assert client.post("/s/ON", data={"name": "Maya", "occasion": "free text"}).status_code == 400
    assert client.post("/s/ON", data={"name": "Maya", "occasion": "birthday", "website": "x"}).status_code == 200
    assert conn.execute("SELECT COUNT(*) n FROM shoutouts").fetchone()["n"] == 0


def test_synthesize_uses_key_and_needs_config(tmp_path):
    seen = {}

    def handler(req):
        seen["key"], seen["url"] = req.headers["xi-api-key"], str(req.url)
        return httpx.Response(200, content=b"ID3audio")
    out = drops.synthesize("Hi", "k", "voice1", tmp_path / "a.mp3", httpx.MockTransport(handler))
    assert out.read_bytes() == b"ID3audio" and seen["key"] == "k" and "voice1" in seen["url"]
    with pytest.raises(ValueError):
        drops.synthesize("Hi", "", "", tmp_path / "b.mp3")


# ---- wrapped ----
def test_wrapped_real_numbers_only(tmp_path):
    tracks = wrapped.read_history("Track Title\tArtist\nA\tX\nB\tY\n")
    assert len(tracks) == 2
    items = wrapped.stats(tracks, 240, "12:41 AM", 8)
    assert items[0] == ("2", "tracks played") and items[1] == ("4", "hours on the decks")
    assert wrapped.stats([], None, None, None) == []
    with pytest.raises(ValueError):
        wrapped.render("Rho Sigma", "Nov 14", [], tmp_path / "x.png")
    p = wrapped.render("Omega Psi Phi Rho Sigma Chapter Founders' Day", "Sat Nov 14, 2026", items, tmp_path / "w.png")
    assert Image.open(p).size == (1080, 1920)


def test_admin_opens_and_closes_shoutouts(client):
    assert client.post("/admin/shouts/HALL/open", data={"name": "Night"}).status_code == 401
    assert client.post("/admin/shouts/bad code!/open", auth=AUTH).status_code == 400
    assert client.post("/admin/shouts/HALL/open", data={"name": "Night"}, auth=AUTH).json()["url"].endswith("/s/HALL")
    assert client.get("/s/HALL").status_code == 200
    client.post("/admin/shouts/HALL/close", auth=AUTH)
    assert client.get("/s/HALL").status_code == 404


def test_app_loads_all_chapters_and_demand_page(client):
    page = client.get("/admin/demand", auth=AUTH)
    assert page.status_code == 200 and "@" in page.text
    assert client.get("/admin/demand").status_code == 401


def test_cli_drop_manifest_and_crate(tmp_path, conn):
    from valagents import cli
    out = tmp_path / "drops.csv"
    assert cli.main(["drops", "--out", str(out)]) == 0
    rows = out.read_text().splitlines()
    assert len(rows) == 1 + 53 and "chapter" in rows[1] or len(rows) == 54
    xml = tmp_path / "lib.xml"
    xml.write_bytes(lib())
    result = tmp_path / "crate.xml"
    assert cli.main(["crate", str(xml), "--minutes", "120", "--name", "Test", "--dnp", "Artist 1", "--out", str(result)]) == 0
    assert b"Test" in result.read_bytes()
    hist = tmp_path / "h.txt"
    hist.write_text("Track Title\tArtist\nA\tB\n")
    png = tmp_path / "c.png"
    assert cli.main(["card", "--host", "Host", "--date", "Nov 14", "--history", str(hist), "--minutes", "240", "--out", str(png)]) == 0
    assert png.exists()
