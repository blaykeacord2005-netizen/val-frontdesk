# DJ Val front desk

One app, one CRM, and a human approval step before anything reaches a client. Built by Remnant Co. The app is ready to deploy; nothing here is live yet.

## What's in this build

| Piece | What it does | State as shipped |
|---|---|---|
| Voice front desk (Volt, ElevenLabs over Twilio) | Takes booking details by phone and saves them | Backend tested on fake data. No real call made |
| Missed-call handling | A missed or fast-hung-up call becomes an inquiry and an email to Val | Tested with signed fake callbacks. No real Twilio callback yet |
| Email booking intake | Reads the booking inbox read-only, pulls out the details, queues them for Val | Tested on a local fake inbox. Off until the inbox details are set |
| Clash check | Compares a date to Val's calendar and existing holds | Built and tested, switched OFF |
| Quote approval | One-time link for Val, quote stays a draft until he approves it | Built and tested, switched OFF |
| Gig tracker | Daily one-way export from the CRM to two Sheet tabs | Tested against a fake Google endpoint. Off until a sheet is set |

Not in this build: Name Drops, Party Wrapped, Night Card, Live Shoutouts, Demand Desk and Crate Builder are moved to `parked/` and aren't imported. Text-back isn't built: SMS waits on A2P approval.

## Rules built in
- Volt has no tool to confirm a booking, quote a price or take money. While switches are off it can't say a date is free or hold one.
- Client messages are drafts until approved. The app never auto-replies to an email.
- The mailbox is opened read-only (EXAMINE and BODY.PEEK). It can't mark, move or delete mail. Only mail on or after `EMAIL_START_DATE` is read.
- Mail that isn't a booking keeps no content, only a hash so it isn't reread.
- Email text is untrusted: it can only fill validated fields and can't overwrite what's on file. Differences go to notes.
- Dates are only taken when written out. "Next Saturday" and a wrong weekday get flagged, not guessed.
- The Sheet is output only. The app never reads it, so the CRM is the one place to write.
- `DRY_RUN=1` is the default. While it's on, alerts are logged instead of emailed.

## Switches
Each one needs both the switch in `data/val_config.json` and the real data.

| Feature | Turns on when |
|---|---|
| Clash check | `features.clash_check` true and `VAL_ICS_URL` set |
| Quote approval | `features.quote_approval` true and `rates_confirmed` true, after Val's real rates are in `data/val_config.json` |
| Holds | Both of the above |
| Email intake | `EMAIL_INTAKE_ENABLED=1` plus `VAL_BOOKING_EMAIL`, `IMAP_USER`, `IMAP_APP_PASSWORD`, `EMAIL_START_DATE` |

`python3 -m valagents.preflight` prints what's set, what's a placeholder and what's off. It prints no secrets.

## Run and test it
```
pip install -r requirements.txt
python3 -m pytest -q
python3 demo/run_e2e.py     # fake data, starts its own server, inbox and calendar
```
Admin screen: `/admin` (any username, `ADMIN_PASSWORD`).

## Deploy (Railway)
1. New service from this code, a volume at `/data`, `DB_PATH=/data/val.db`, and the variables in `.env.example`. Keep `DRY_RUN=1`.
2. Schedule POST `/cron/daily` once a day and POST `/cron/email` every 15 minutes, each with header `X-Cron-Secret`.
3. Set up the voice line with `voice/agent_setup.md`.
4. Gig tracker: make a Google service account, share the sheet with its email as an editor, and add two tabs named exactly `Gigs (auto, do not edit)` and `Inquiries (auto, do not edit)`. Anything typed in those tabs gets overwritten.

## Known limits
- A recurring calendar event only blocks its first date.
- Clash check can't see plans that aren't on Val's calendar.
- The email reader works from fixed rules unless `ANTHROPIC_API_KEY` is set, and will miss some wording. Anything uncertain goes to review rather than being dropped.
- Unverified until a live test: a real phone call, Twilio's status callback alongside ElevenLabs' number import, Val's real mail provider, and a real Sheets write.
