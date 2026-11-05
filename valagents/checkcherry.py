"""Optional sync to Check Cherry, if Val ends up paying for it. Local SQLite stays the source of truth.
Only uses what Check Cherry's public API docs show: POST /leads (email required) with an Api-Key header.
NOT tested against a live account yet; tests use a fake server."""
from __future__ import annotations

import httpx

BASE = "https://api.checkcherry.com/api/v1"


class CheckCherry:
    def __init__(self, key: str, transport: httpx.BaseTransport | None = None):
        if not key:
            raise ValueError("CHECKCHERRY_API_KEY is missing")
        self.client = httpx.Client(base_url=BASE, headers={"Api-Key": key}, timeout=15, transport=transport)

    def create_lead(self, email: str, name: str | None = None, phone: str | None = None) -> str | None:
        if not email:
            raise ValueError("Check Cherry requires an email to create a lead")
        attrs = {"email": email}
        if name:
            attrs["name"] = name
        if phone:
            attrs["phone"] = phone
        resp = self.client.post("/leads", json=attrs)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return str(data.get("id")) if data.get("id") is not None else None
