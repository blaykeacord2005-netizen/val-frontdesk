import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from valagents.app import create_app  # noqa: E402
from valagents.settings import Settings  # noqa: E402

SECRET = "s" * 32
AUTH = ("admin", "a" * 32)


def next_saturday(weeks: int = 2) -> str:
    d = date.today() + timedelta(days=7 * weeks)
    return (d + timedelta(days=(5 - d.weekday()) % 7)).isoformat()


@pytest.fixture
def settings(tmp_path):
    base = json.loads((Path(__file__).parent.parent / "data" / "val_config.json").read_text())
    base["rates_confirmed"] = True
    base["features"] = {"clash_check": True, "quote_approval": True, "voice_direct_hold": True}
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(base))
    return Settings(db_path=str(tmp_path / "t.db"), secret_key=SECRET, public_url="http://testserver", dry_run=True,
                    voice_secret=SECRET, admin_password=AUTH[1], cron_secret=SECRET, config_path=str(cfg),
                    anthropic_key="", elevenlabs_key="", ics_url="", alert_val="val@example.com",
                    alert_blayke="blayke@example.com", alert_max="max@example.com", alert_darrell="d@example.com")


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings, busy_source=lambda: []), raise_server_exceptions=True)


@pytest.fixture
def conn(client):
    return client.app.state.conn


VH = {"X-Voice-Secret": SECRET}


def hold(client, day=None, start="22:00", end="02:00", **over):
    payload = {"caller_name": "Maya Jones", "organization": "Omega Psi Phi Rho Sigma", "role": "social chair", "event_type": "party",
               "date": day or next_saturday(), "start_time": start, "end_time": end, "venue": "The Barn", "campus": "Purdue",
               "headcount": "150", "phone": "(765) 555-0100", "email": "maya@example.com"}
    payload.update(over)
    return client.post("/voice/tools/hold_date", json=payload, headers=VH).json()


def links_in_alert(conn, booking_id):
    body = conn.execute("SELECT body FROM outbox WHERE kind='alert' AND booking_id=? AND dedupe_key LIKE 'hold-alert%'", (booking_id,)).fetchone()["body"]
    import re
    found = re.findall(r"/a/([\w\-.]+)", body)
    return found[0], found[1]
