from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    value = _as_utc(value)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _day_boundaries(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    return yesterday, today, tomorrow


def view_redeemed_tickets(*, ticket_class, render, now: datetime | None = None):
    yesterday, today, tomorrow = _day_boundaries(now)
    owner_groups = defaultdict(lambda: {"owner": "", "today_count": 0, "yesterday_count": 0, "tickets": []})

    tickets = sorted(
        ticket_class.objects(redeemed__dt__gte=yesterday, redeemed__dt__lt=tomorrow),
        key=lambda ticket: (ticket.owner, _as_utc(ticket.redeemed.dt)),
    )
    for ticket in tickets:
        redeemed = ticket.redeemed
        owner = ticket.owner
        group = owner_groups[owner]
        group["owner"] = owner
        redeemed_dt = _as_utc(redeemed.dt)
        day = "today" if redeemed_dt >= today else "yesterday"
        group[f"{day}_count"] += 1
        group["tickets"].append(
            {
                "code": ticket.code,
                "day": day,
                "tooltip": "; ".join(
                    [
                        f"Reason: {redeemed.reason or 'unknown'}",
                        f"Redeemed by: {redeemed.by_user or 'unknown'}",
                        f"Redeemed at: {_format_datetime(redeemed.dt)}",
                    ]
                ),
            }
        )

    return render(
        "report_redeemed_tickets.html",
        owners=sorted(owner_groups.values(), key=lambda group: group["owner"]),
    )
