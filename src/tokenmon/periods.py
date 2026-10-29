"""Per-day and per-week usage: the official account percent, and this device's own part of it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import credits
from .core import Store

WEEK = timedelta(days=7)


def when(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def local_midnight(now: datetime) -> datetime:
    """Start of the local calendar day containing `now`, as UTC."""
    return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def credits_since(store: Store, provider: str, start: datetime) -> float:
    """Credits this device spent since `start`: tokens (cache included) x the model's rate, as Claude Code counts."""
    rates = credits.credit_rates()
    by_model = store.device_tokens_by_model(provider, start.isoformat())
    return sum(tokens * rates.get(model, 0) / 1e6 for model, tokens in by_model.items())


def period_report(store: Store, provider: str, now: datetime | None = None) -> dict[str, Any]:
    """Usage for today and this weekly window.

    `official` is the account's figure (all devices, products and surfaces; today's is the change since the
    last reading at or before local midnight). `pct` is this device alone. For Claude it is the credits this
    device spent divided by the plan's weekly credit capacity. Codex has no credit table, and with an unknown
    plan the capacity is unknown too; in both cases `pct` falls back to the account figure (`basis` says so).
    """
    now = now or datetime.now(timezone.utc)
    latest = {r["allowance_type"]: r for r in store.latest_allowances() if r["provider"] == provider}
    week_row = latest.get("seven_day")

    week_reset = when(week_row["resets_at"]) if week_row else None
    week_live = week_reset is not None and week_reset > now
    week_start = week_reset - WEEK if week_live else now - WEEK
    midnight = local_midnight(now)
    day_start = max(midnight, week_start)
    week_pct = None
    if week_live and week_row["remaining_percent"] is not None:
        week_pct = 100.0 - float(week_row["remaining_percent"])

    day_pct, note = None, ""
    if week_pct is not None:
        before = store.readings(provider, "seven_day", week_start.isoformat(), midnight.isoformat())
        base = None
        if before:
            base, note = 100.0 - float(before[-1]["remaining_percent"]), "sejak 00:00"
        elif week_start >= midnight:
            base, note = 0.0, "sejak reset"
        else:  # no reading from before midnight: say honestly where the change is measured from
            today = store.readings(provider, "seven_day", midnight.isoformat(), now.isoformat())
            if today:
                base = 100.0 - float(today[0]["remaining_percent"])
                note = "sejak " + when(today[0]["first_seen_at"]).astimezone().strftime("%H:%M")
        if base is not None:
            day_pct = min(week_pct, max(0.0, week_pct - base))

    capacity = credits.weekly_capacity() if provider == "claude" else None
    week_credits = credits_since(store, provider, week_start) if capacity else None
    day_credits = credits_since(store, provider, day_start) if capacity else None

    def mine(start: datetime) -> int:
        """This device's tokens since `start`; peers' tokens (after a sync) are not counted."""
        return store.tokens_by_device(provider, start.isoformat()).get(store.device_id, 0)

    name = store.db.execute("SELECT display_name FROM devices WHERE id=?", (store.device_id,)).fetchone()
    return {
        "day": {"tokens": mine(day_start), "official": day_pct, "note": note,
                "pct": day_credits / capacity * 100 if capacity else day_pct},
        "week": {"tokens": mine(week_start), "official": week_pct,
                 "reset": week_row["resets_at"] if week_live else None,
                 "pct": week_credits / capacity * 100 if capacity else week_pct},
        "device": {"name": name[0] if name else "perangkat ini", "credits": week_credits, "capacity": capacity,
                   "basis": "kredit" if capacity else "akun"},
    }
