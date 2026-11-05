"""Web app: voice tools, approval links, client intake, guest shoutouts and Val's admin screen."""
from __future__ import annotations

import base64
import hmac
import html
import json
import re
import sqlite3
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from . import crm, email_intake, followups, gigsheet, inquiries, messaging, missed_calls, onboarding, sheet_export, voice
from .availability import parse_ics
from .db import connect, utcnow
from .links import BadLink, check, consume
from .money import dollars
from .settings import TZ, Settings

STYLE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         "<style>:root{color-scheme:dark}body{font:16px system-ui;max-width:680px;margin:auto;padding:16px;background:#0b0b10;color:#f4f4f6}"
         "a{color:#c7ff3e}input,select,textarea{width:100%;padding:10px;margin:4px 0 12px;background:#17171f;color:#fff;border:1px solid #333;border-radius:8px;box-sizing:border-box}"
         "button{padding:12px 18px;background:#c7ff3e;color:#000;border:0;border-radius:8px;font-weight:700;margin:4px 8px 4px 0}"
         "button.no{background:#2a2a35;color:#fff}.card{border:1px solid #2a2a35;border-radius:10px;padding:12px;margin:10px 0}.dim{color:#a7a7b5}.hp{position:absolute;left:-9999px}"
         "pre{white-space:pre-wrap;font:inherit}</style>")


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"{STYLE}<title>{html.escape(title)}</title><h1>{html.escape(title)}</h1>{body}", status)


def create_app(settings: Settings | None = None, busy_source=None) -> FastAPI:
    from .settings import load

    s = settings or load()
    app = FastAPI(title="Val Agents", docs_url=None, redoc_url=None, openapi_url=None)
    conn: sqlite3.Connection = connect(s.db_path)
    app.state.conn, app.state.settings = conn, s

    has_calendar = bool(busy_source or s.ics_url)

    def busy():
        if busy_source:
            return busy_source()
        if not s.ics_url:
            return []
        import httpx

        # Fail closed: if the calendar can't be read, the tools say they can't confirm the date is free.
        resp = httpx.get(re.sub(r"(?i)^webcal://", "https://", s.ics_url), timeout=8, follow_redirects=True)
        resp.raise_for_status()
        return parse_ics(resp.text)

    def cfg() -> dict:
        return s.business()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Reject cross-site form posts to the admin area (basic auth alone doesn't stop them).
        if request.method == "POST" and request.url.path.startswith("/admin"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
        return resp

    def need_secret(expected: str, given: str | None) -> None:
        if len(expected) < 16 or not given or not hmac.compare_digest(expected, given):
            raise HTTPException(401, "Unauthorized")

    def voice_auth(x_voice_secret: str | None = Header(default=None)) -> None:
        need_secret(s.voice_secret, x_voice_secret)

    def admin_auth(authorization: str | None = Header(default=None)) -> None:
        challenge = {"WWW-Authenticate": 'Basic realm="val"'}
        if len(s.admin_password) < 16 or not authorization or not authorization.startswith("Basic "):
            raise HTTPException(401, "Unauthorized", challenge)
        try:
            _, password = base64.b64decode(authorization[6:]).decode().split(":", 1)
        except Exception:
            raise HTTPException(401, "Unauthorized", challenge)
        if not hmac.compare_digest(password, s.admin_password):
            raise HTTPException(401, "Unauthorized", challenge)

    @app.get("/health")
    def health():
        conn.execute("SELECT 1").fetchone()
        c = cfg()
        return {"ok": True, "dry_run": s.dry_run, "missing_secrets": len(s.validate_for_production()),
                "clash_check": s.clash_on(c, has_calendar), "quote_approval": s.quote_on(c), "email_intake": s.email_intake_on()}

    # ---------- Voice agent tools ----------
    @app.post("/voice/init", dependencies=[Depends(voice_auth)])
    async def voice_init(request: Request):
        data = await request.json()
        return voice.init_payload(cfg(), data.get("caller_id"), utcnow())

    def tool_busy():
        try:
            return busy(), None
        except Exception:
            return None, {"ok": False, "error": "Can't read Val's calendar right now. Take a message instead of holding a date."}

    @app.post("/voice/tools/check_availability", dependencies=[Depends(voice_auth)])
    async def voice_check(request: Request):
        d = await request.json()
        c = cfg()
        if not s.clash_on(c, has_calendar):
            return voice.availability_off()
        b, err = tool_busy()
        return err or voice.check_availability(conn, c, str(d.get("date", "")), str(d.get("start_time", "")), str(d.get("end_time", "")), b)

    @app.post("/voice/tools/hold_date", dependencies=[Depends(voice_auth)])
    async def voice_hold(request: Request):
        d = await request.json()
        c = cfg()
        if not (s.clash_on(c, has_calendar) and s.quote_on(c)):
            return voice.save_inquiry(conn, s, c, d)
        b, err = tool_busy()
        return err or voice.hold_date(conn, s, c, d, b)

    @app.post("/voice/tools/save_inquiry", dependencies=[Depends(voice_auth)])
    async def voice_save(request: Request):
        return voice.save_inquiry(conn, s, cfg(), await request.json())

    @app.post("/voice/tools/take_message", dependencies=[Depends(voice_auth)])
    async def voice_message(request: Request):
        return voice.take_message(conn, s, await request.json())

    @app.post("/voice/post_call", dependencies=[Depends(voice_auth)])
    async def voice_post(request: Request):
        return voice.post_call(conn, s, await request.json())

    # ---------- Val's signed approval links: GET shows the page, only POST acts ----------
    ACTIONS = ("approve_quote", "decline")

    @app.get("/a/{token}", response_class=HTMLResponse)
    def approval_page(token: str):
        try:
            link = check(conn, s.secret_key, token, ACTIONS)
        except BadLink as exc:
            return page("Link problem", f"<p>{html.escape(str(exc))}</p>", 400)
        b = crm.get(conn, link["booking_id"])
        verb = "Create the quote draft" if link["action"] == "approve_quote" else "Decline this booking"
        return page(verb, f"<div class=card><b>{html.escape(b['org'])}</b><br>{html.escape(crm.pretty_when(b))}<br>"
                          f"<span class=dim>Stage: {html.escape(b['stage'])}</span></div>"
                          f"<form method=post><button>{verb}</button></form>")

    @app.post("/a/{token}", response_class=HTMLResponse)
    def approval_act(token: str):
        try:
            link = check(conn, s.secret_key, token, ACTIONS)
            booking_id = link["booking_id"]
            if link["action"] == "approve_quote":
                b, err = tool_busy()
                if err:
                    return page("Not yet", "<p>Couldn't read the calendar. Nothing was sent. Try again in a minute.</p>", 503)
                # Validate first, use the link only on success, so a fixable problem doesn't burn it.
                c = cfg()
                if not s.quote_on(c):
                    raise crm.CrmError("Quote approval is switched off until Val's real rates are in. Nothing was sent.")
                result = crm.approve_quote(conn, s, c, booking_id, "val", busy=b)
                consume(conn, s.secret_key, token, ACTIONS)
                q = result["quote"]
                return page("Quote drafted", f"<p>Total {dollars(q.total_cents)}, deposit {dollars(q.deposit_cents)}. "
                                             "Review and send it from your admin screen. Nothing has gone to the client yet.</p>")
            crm.decline(conn, booking_id, "val")
            consume(conn, s.secret_key, token, ACTIONS)
            return page("Declined", "<p>The date is open again. Nothing was sent.</p>")
        except (BadLink, crm.CrmError) as exc:
            return page("Not done", f"<p>{html.escape(str(exc))}</p>", 409)

    # ---------- Client intake ----------
    def intake_form(token: str, booking, note: str = "") -> HTMLResponse:
        details = json.loads(booking["details"] or "{}")
        fields = ""
        for key, (label, kind) in onboarding.FIELDS.items():
            cur = details.get(key)
            value = "yes" if cur is True else "no" if cur is False else "" if cur is None else str(cur)
            fields += f"<label>{html.escape(label)}<input name='{key}' value='{html.escape(value, quote=True)}'></label>"
        return page(f"Event details: {booking['org']}",
                    f"{note}<p class=dim>{html.escape(crm.pretty_when(booking))}</p>"
                    f"<form method=post enctype=multipart/form-data><input class=hp name=website tabindex=-1 autocomplete=off>{fields}"
                    "<label>Paperwork (PDF or TXT, optional)<input type=file name=document accept='.pdf,.txt'></label><button>Send details</button></form>")

    @app.get("/onboard/{token}", response_class=HTMLResponse)
    def onboard_page(token: str):
        try:
            link = check(conn, s.secret_key, token, ("onboard",))
        except BadLink as exc:
            return page("Link problem", f"<p>{html.escape(str(exc))}</p>", 400)
        return intake_form(token, crm.get(conn, link["booking_id"]))

    @app.post("/onboard/{token}", response_class=HTMLResponse)
    async def onboard_submit(token: str, request: Request):
        try:
            link = check(conn, s.secret_key, token, ("onboard",))
        except BadLink as exc:
            return page("Link problem", f"<p>{html.escape(str(exc))}</p>", 400)
        booking_id = link["booking_id"]
        form = await request.form()
        if form.get("website"):
            return page("Thanks", "<p>Got it.</p>")
        fields = {k: c for k in onboarding.FIELDS if (c := onboarding.coerce(k, form.get(k))) is not None}
        result = onboarding.apply_fields(conn, s, booking_id, fields, "client_form", "form")
        upload = form.get("document")
        if upload is not None and getattr(upload, "filename", ""):
            blob = await upload.read(onboarding.MAX_BYTES + 1)
            try:
                result = onboarding.ingest_document(conn, s, booking_id, upload.filename, blob)
            except ValueError as exc:
                return intake_form(token, crm.get(conn, booking_id), f"<p>{html.escape(str(exc))}</p>")
        missing = result.get("missing")
        if missing:
            note = "<p>Saved. Still needed: " + html.escape(", ".join(onboarding.FIELDS[m][0] for m in missing)) + "</p>"
            return intake_form(token, crm.get(conn, booking_id), note)
        return page("You're all set", "<p>Thanks. Val has everything he needs.</p>")

    # ---------- Admin ----------
    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(admin_auth)])
    def admin():
        rep = followups.report(conn)
        rows = conn.execute("SELECT * FROM bookings WHERE stage IN ('held','quoted','confirmed') ORDER BY start_utc").fetchall()
        cards = ""
        for b in rows:
            action = ""
            if b["stage"] == "quoted":
                action = f"<form method=post action=/admin/bookings/{b['id']}/deposit><button>Deposit received</button></form>"
            cards += (f"<div class=card><b>{html.escape(b['org'])}</b> · {html.escape(b['stage'])}<br>{html.escape(crm.pretty_when(b))}"
                      f"<br><a href=/admin/gigsheet/{b['id']}>Gig sheet</a>{action}</div>")
        c = cfg()
        hold_on = s.clash_on(c, has_calendar) and s.quote_on(c)
        icards = ""
        for q in inquiries.open_list(conn):
            missing = json.loads(q["missing"] or "[]")
            hold_btn = f"<form method=post action=/admin/inquiries/{q['id']}/hold style=display:inline><button>Hold date</button></form>" if hold_on and q["status"] == "ready" else ""
            icards += (f"<div class=card><b>{html.escape(q['org'] or q['from_name'] or q['from_phone'] or 'Unknown')}</b> · {html.escape(q['channel'])} · {html.escape(q['status'])}<br>"
                       f"{html.escape(' · '.join(x for x in (q['from_name'], q['from_email'], q['from_phone']) if x) or 'no contact details')}<br>"
                       f"{html.escape(q['event_date'] or 'no date')} {html.escape(q['start_time'] or '')}-{html.escape(q['end_time'] or '')}<br>"
                       f"{html.escape(' · '.join(str(x) for x in (q['venue'], q['campus'], q['headcount'] and str(q['headcount']) + ' people') if x))}<br>"
                       f"<span class=dim>Date check: {html.escape(q['clash_status'])}"
                       f"{' · Missing: ' + html.escape(', '.join(missing)) if missing else ''}</span><pre>{html.escape(q['excerpt'] or '')}</pre>{hold_btn}"
                       f"<form method=post action=/admin/inquiries/{q['id']}/decline style=display:inline><button class=no>Close</button></form></div>")
        drafts = conn.execute("SELECT * FROM outbox WHERE status='draft' ORDER BY id").fetchall()
        dcards = "".join(
            f"<div class=card><b>{html.escape(d['kind'])}</b> to {html.escape(d['to_addr'] or 'nobody')} ({html.escape(d['channel'])})"
            f"<pre>{html.escape(d['body'])}</pre><form method=post action=/admin/outbox/{d['id']}/approve style=display:inline><button>Approve</button></form>"
            f"<form method=post action=/admin/outbox/{d['id']}/reject style=display:inline><button class=no>Reject</button></form></div>" for d in drafts)
        return page("DJ Val: control room",
                    f"<p class=dim>Booked: {dollars(rep['booked_cents'])} · Drafts waiting: {rep['drafts_waiting']}"
                    f" · Dry run: {'on' if s.dry_run else 'off'} · Holds and quotes: {'on' if hold_on else 'OFF'}</p><h2>Waiting on you</h2>{dcards or '<p class=dim>Nothing.</p>'}"
                    f"<h2>Inquiries</h2>{icards or '<p class=dim>None open.</p>'}"
                    f"<h2>Pipeline</h2>{cards or '<p class=dim>No open bookings.</p>'}<p><a href=/admin/report.json>Numbers</a></p>")

    @app.post("/admin/bookings/{booking_id}/deposit", dependencies=[Depends(admin_auth)], response_class=HTMLResponse)
    def admin_deposit(booking_id: int):
        try:
            crm.mark_deposit(conn, booking_id, "admin")
        except crm.CrmError as exc:
            return page("Not done", f"<p>{html.escape(str(exc))}</p>", 409)
        onboarding.start(conn, s, booking_id)
        return page("Confirmed", "<p>Onboarding started. The intake draft is on your admin screen.</p><a href=/admin>Back</a>")

    @app.post("/admin/outbox/{outbox_id}/approve", dependencies=[Depends(admin_auth)], response_class=HTMLResponse)
    def admin_approve(outbox_id: int):
        try:
            status = messaging.approve(conn, s, outbox_id, "admin")
        except ValueError as exc:
            return page("Not done", f"<p>{html.escape(str(exc))}</p>", 409)
        row = conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        extra = f"<p>Copy and send this yourself:</p><pre>{html.escape(row['body'])}</pre>" if status == "copy_and_send" else ""
        return page("Approved", f"<p>Status: {html.escape(status)}</p>{extra}<a href=/admin>Back</a>")

    @app.post("/admin/outbox/{outbox_id}/reject", dependencies=[Depends(admin_auth)], response_class=HTMLResponse)
    def admin_reject(outbox_id: int):
        try:
            messaging.reject(conn, outbox_id, "admin")
        except ValueError as exc:
            return page("Not done", f"<p>{html.escape(str(exc))}</p>", 409)
        return page("Rejected", "<a href=/admin>Back</a>")

    @app.get("/admin/gigsheet/{booking_id}", dependencies=[Depends(admin_auth)], response_class=HTMLResponse)
    def admin_gigsheet(booking_id: int):
        try:
            b = crm.get(conn, booking_id)
        except crm.CrmError:
            raise HTTPException(404, "Not found")
        return gigsheet.render(b, crm.contact_of(conn, b), cfg())

    @app.post("/admin/inquiries/{inquiry_id}/hold", dependencies=[Depends(admin_auth)], response_class=HTMLResponse)
    def admin_inquiry_hold(inquiry_id: int):
        c = cfg()
        if not (s.clash_on(c, has_calendar) and s.quote_on(c)):
            return page("Switched off", "<p>Holds stay off until Val's real rates and iCal link are in. Nothing was held or sent.</p><a href=/admin>Back</a>", 409)
        b, err = tool_busy()
        if err:
            return page("Not yet", "<p>Couldn't read the calendar. Nothing was held.</p>", 503)
        try:
            result = inquiries.promote_to_hold(conn, s, c, inquiry_id, busy=b)
        except (ValueError, crm.CrmError) as exc:
            return page("Not done", f"<p>{html.escape(str(exc))}</p>", 409)
        return page("Held", f"<p>Booking #{result['booking_id']} is on hold. Check your email for the quote link.</p><a href=/admin>Back</a>")

    @app.post("/admin/inquiries/{inquiry_id}/decline", dependencies=[Depends(admin_auth)], response_class=HTMLResponse)
    def admin_inquiry_decline(inquiry_id: int):
        try:
            inquiries.close(conn, inquiry_id, "declined")
        except ValueError as exc:
            return page("Not done", f"<p>{html.escape(str(exc))}</p>", 409)
        return page("Closed", "<p>Nothing was sent.</p><a href=/admin>Back</a>")

    @app.get("/admin/report.json", dependencies=[Depends(admin_auth)])
    def admin_report():
        return followups.report(conn)

    @app.post("/cron/daily")
    def cron_daily(x_cron_secret: str | None = Header(default=None)):
        need_secret(s.cron_secret, x_cron_secret)
        out = followups.run_daily(conn, s, cfg(), utcnow())
        out["sheet_export"] = sheet_export.run(conn, s)
        return out

    @app.post("/cron/email")
    def cron_email(x_cron_secret: str | None = Header(default=None)):
        need_secret(s.cron_secret, x_cron_secret)
        c = cfg()
        busy_now = None
        if s.clash_on(c, has_calendar):
            try:
                busy_now = busy()
            except Exception:
                busy_now = None
        return email_intake.poll_inbox(conn, s, c, clash_on=s.clash_on(c, has_calendar), busy=busy_now)

    @app.post("/twilio/status")
    async def twilio_status(request: Request, x_twilio_signature: str | None = Header(default=None)):
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        url = s.public_url + "/twilio/status"
        if not missed_calls.valid_signature(s.twilio_auth_token, url, params, x_twilio_signature):
            raise HTTPException(403, "Bad signature")
        return missed_calls.handle_status(conn, s, cfg(), params)

    return app
