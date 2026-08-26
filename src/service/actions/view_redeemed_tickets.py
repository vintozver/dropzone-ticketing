from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dropzone_ticketing.service.config import local_timezone
from dropzone_ticketing.time_utils import as_utc, format_datetime


def _user_label(ref) -> str:
    if ref is None:
        return ""
    return ref.display_name or (str(ref.id) if ref.id else "")


def _day_boundaries(now: datetime | None = None, display_timezone=None) -> tuple[datetime, datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    display_timezone = display_timezone or local_timezone()
    local_now = as_utc(now).astimezone(display_timezone)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    return as_utc(yesterday), as_utc(today), as_utc(tomorrow)


def view_redeemed_tickets(*, ticket_class, render, now: datetime | None = None):
    display_timezone = local_timezone()
    yesterday, today, tomorrow = _day_boundaries(now, display_timezone)
    owner_groups = defaultdict(lambda: {"owner": "", "today_count": 0, "yesterday_count": 0, "yesterday_tickets": [], "today_tickets": []})

    tickets = sorted(
        ticket_class.objects(redeemed__dt__gte=yesterday, redeemed__dt__lt=tomorrow),
        key=lambda ticket: ((ticket.issued_to.display_name if ticket.issued_to else ""), as_utc(ticket.redeemed.dt)),
    )
    for ticket in tickets:
        redeemed = ticket.redeemed
        owner = (ticket.issued_to.display_name if ticket.issued_to else "") or "unknown"
        group = owner_groups[owner]
        group["owner"] = owner
        redeemed_dt = as_utc(redeemed.dt)
        day = "today" if redeemed_dt >= today else "yesterday"
        group[f"{day}_count"] += 1
        group[f"{day}_tickets"].append(
            {
                "url": f"/ticket/{ticket.id}",
                "tooltip": "; ".join(
                    [
                        f"Reason: {redeemed.reason or ''}",
                        f"By: {_user_label(redeemed.by)}",
                        f"At: {format_datetime(redeemed.dt, display_timezone)}",
                    ]
                ),
            }
        )

    return render(
        "report_redeemed_tickets.html",
        owners=sorted(owner_groups.values(), key=lambda group: group["owner"]),
    )
