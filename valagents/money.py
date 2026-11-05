"""Quote maths in integer cents so rounding can never drift."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


class QuoteError(ValueError):
    pass


@dataclass(frozen=True)
class Quote:
    package: str
    label: str
    base_cents: int
    overtime_cents: int
    travel_cents: int
    total_cents: int
    deposit_cents: int
    balance_cents: int


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


def _cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_quote(cfg: dict, package: str, set_minutes: int, miles: int | None) -> Quote:
    pkg = cfg["packages"].get(package)
    if pkg is None:
        raise QuoteError(f"Unknown package: {package}")
    if set_minutes <= 0:
        raise QuoteError("Set length must be positive")
    if miles is not None and miles < 0:
        raise QuoteError("Miles can't be negative")
    base = _cents(Decimal(str(pkg["price"])))
    # Overtime bills in full hours past the package length; a short set is still the package price.
    extra = max(0, set_minutes - int(pkg["hours"]) * 60)
    extra_hours = (extra + 59) // 60
    overtime = _cents(Decimal(str(cfg["overtime_per_hour"]))) * extra_hours
    travel = 0
    if miles is not None and miles > int(cfg["travel_free_miles"]):
        travel = _cents(Decimal(miles) * 2 * Decimal(str(cfg["travel_per_mile_round_trip"])))
    total = base + overtime + travel
    deposit = _cents(Decimal(total) * Decimal(str(cfg["deposit_percent"])) / Decimal(100) / 100)
    return Quote(package, pkg["label"], base, overtime, travel, total, deposit, total - deposit)
