from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dropzone_ticketing.service.config import local_timezone
from dropzone_ticketing.time_utils import as_utc, format_datetime


def _format_datetime(value: datetime | None) -> str:
    return format_datetime(value, local_timezone())


def _as_utc(value: datetime) -> datetime:
    return as_utc(value)


def _user_label(ref) -> str:
    if ref is None:
        return ""
    return ref.display_name or (str(ref.id) if ref.id else "")


def _day_boundaries(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    local_now = _as_utc(now).astimezone(local_timezone())
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    return _as_utc(yesterday), _as_utc(today), _as_utc(tomorrow)


def view_redeemed_tickets(*, ticket_class, render, now: datetime | None = None):
    yesterday, today, tomorrow = _day_boundaries(now)
    owner_groups = defaultdict(lambda: {"owner": "", "today_count": 0, "yesterday_count": 0, "yesterday_tickets": [], "today_tickets": []})

    tickets = sorted(
        ticket_class.objects(redeemed__dt__gte=yesterday, redeemed__dt__lt=tomorrow),
        key=lambda ticket: ((ticket.issued_to.display_name if ticket.issued_to else ""), _as_utc(ticket.redeemed.dt)),
    )
    for ticket in tickets:
        redeemed = ticket.redeemed
        owner = (ticket.issued_to.display_name if ticket.issued_to else "") or "unknown"
        group = owner_groups[owner]
        group["owner"] = owner
        redeemed_dt = _as_utc(redeemed.dt)
        day = "today" if redeemed_dt >= today else "yesterday"
        group[f"{day}_count"] += 1
        group[f"{day}_tickets"].append(
            {
                "url": f"/ticket/{ticket.id}",
                "tooltip": "; ".join(
                    [
                        f"Reason: {redeemed.reason or ''}",
                        f"By: {_user_label(redeemed.by)}",
                        f"At: {_format_datetime(redeemed.dt)}",
                    ]
                ),
            }
        )

    return render(
        "report_redeemed_tickets.html",
        owners=sorted(owner_groups.values(), key=lambda group: group["owner"]),
    )
