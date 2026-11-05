"""Live run against the real server: phone call to gig sheet. Uses fake people. Nothing leaves the machine."""
import json, os, re, sqlite3, sys
from datetime import date, timedelta
import httpx

base = os.environ["BASE"]; secret = os.environ["VOICE_TOOL_SECRET"]; pw = os.environ["ADMIN_PASSWORD"]; db = os.environ["DB_PATH"]
vh = {"X-Voice-Secret": secret}; auth = ("admin", pw); c = httpx.Client(base_url=base, timeout=15)
d = date.today() + timedelta(days=16); d += timedelta(days=(5 - d.weekday()) % 7)
step = lambda n, t: print(f"\n[{n}] {t}")
q = lambda sql, *a: sqlite3.connect(db).execute(sql, a).fetchall()

step(1, "Call comes in. Volt gets today's date and the caller's number")
print(c.post("/voice/init", json={"caller_id": "+17655550100"}, headers=vh).json()["dynamic_variables"])
step(2, f"Caller asks for {d:%A %b %d}, 10pm to 2am")
print(c.post("/voice/tools/check_availability", json={"date": str(d), "start_time": "22:00", "end_time": "02:00"}, headers=vh).json())
step(3, "Caller says yes. Volt places a hold")
r = c.post("/voice/tools/hold_date", headers=vh, json={"caller_name": "Test Caller", "organization": "Omega Psi Phi Rho Sigma", "role": "social chair", "event_type": "Founders Day party",
    "date": str(d), "start_time": "22:00", "end_time": "02:00", "venue": "Test Hall", "campus": "Purdue", "headcount": "150", "phone": "765-555-0100", "email": "caller@example.com"}).json(); print(r)
bid = r["booking_id"]
print("A second caller tries the same night:", c.post("/voice/tools/hold_date", headers=vh, json={"caller_name": "Other", "organization": "Other Org", "date": str(d), "start_time": "21:00", "end_time": "01:00", "phone": "7655550155"}).json())
step(4, "Val gets this alert")
subject, body = q("SELECT subject,body FROM outbox WHERE kind='alert' AND booking_id=?", bid)[0]; print(subject); print(body)
token = re.findall(r"/a/([\w\-.]+)", body)[0]
step(5, "Val opens the link (nothing happens), then taps the button")
print("GET", c.get(f"/a/{token}").status_code, "| stage:", q("SELECT stage FROM bookings WHERE id=?", bid)[0][0])
print("POST", c.post(f"/a/{token}").status_code, "| stage:", q("SELECT stage FROM bookings WHERE id=?", bid)[0][0])
print("Second tap:", c.post(f"/a/{token}").status_code)
step(6, "Quote is a draft. The client has received nothing")
oid, st, b = q("SELECT id,status,body FROM outbox WHERE kind='quote'")[0]; print(st); print(b)
print("Val approves:", c.post(f"/admin/outbox/{oid}/approve", auth=auth).status_code, q("SELECT status FROM outbox WHERE id=?", oid)[0][0])
step(7, "Deposit lands. A person records it. Onboarding starts")
print(c.post(f"/admin/bookings/{bid}/deposit", auth=auth).status_code, q("SELECT stage FROM bookings WHERE id=?", bid)[0][0])
ib = q("SELECT status,body FROM outbox WHERE kind='intake'")[0]; print(ib[0], "|", ib[1])
t2 = re.search(r"/onboard/([\w\-.]+)", ib[1])[1]
step(8, "Client uploads paperwork. The agent reads it, updates the record and alerts the team")
doc = b"Venue: Test Hall\nAddress: 100 Test St, West Lafayette\nLoad-in: 8:30 pm\nEvent contact: Jay Test\nContact phone: 765-555-0199\nExpected headcount: 180\nTable provided: yes\nPower within 25 feet: yes\nHouse sound: no\nCOI required: yes\nAdditional insured: Rho Sigma Chapter\nChapter name OK: yes\nFilming OK: yes\nDo-not-play: Test Song\n"
print(c.post(f"/onboard/{t2}", files={"document": ("event.txt", doc, "text/plain")}).status_code)
print(json.dumps(json.loads(q("SELECT details FROM bookings WHERE id=?", bid)[0][0]), indent=0))
for a in q("SELECT to_addr,subject,body FROM outbox WHERE kind='alert' AND booking_id=? ORDER BY id", bid)[1:]: print("ALERT ->", a[0], "|", a[1])
step(9, "Gig sheet")
html = c.get(f"/admin/gigsheet/{bid}", auth=auth).text
print(re.sub(r"\s+", " ", re.sub(r"<style.*?</style>|<[^>]+>", " ", html, flags=re.S))[:900])
step(10, "Daily run")
print(c.post("/cron/daily", headers={"X-Cron-Secret": os.environ["CRON_SECRET"]}).json())
print("Numbers:", c.get("/admin/report.json", auth=auth).json())
