from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...model.auth import User
from ... import Ticket
from ...time_utils import as_utc
from ..config import local_timezone


def day_boundaries(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    display_timezone = local_timezone()
    local_now = as_utc(now).astimezone(display_timezone)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    return as_utc(yesterday), as_utc(today), as_utc(tomorrow)


def ticket_redeem_report(partner, *, start: datetime, end: datetime):
    tickets = list(
        Ticket.objects(redeemed__dt__gte=start, redeemed__dt__lt=end)
        .only("id", "issued_to", "payment", "purpose", "redeemed")
        .order_by("redeemed__dt")
    )
    user_ids = {ticket.issued_to.id for ticket in tickets if ticket.issued_to and ticket.issued_to.id is not None}
    users_by_id = {}
    if user_ids:
        users_by_id = {
            str(user.id): user
            for user in User.objects(id__in=user_ids).only("id", "display_name", "partner_uid_map")
        }

    groups = {}
    for ticket in tickets:
        redeemed = ticket.redeemed
        issued_to = ticket.issued_to
        internal_id = str(issued_to.id) if issued_to and issued_to.id is not None else None
        user = users_by_id.get(internal_id) if internal_id else None
        external_id = (user.partner_uid_map or {}).get(str(partner.id)) if user is not None else None
        display_name = (user.display_name if user is not None else None) or (issued_to.display_name if issued_to else None)
        if internal_id is not None:
            group_key = ("id", internal_id)
        elif display_name:
            group_key = ("name", display_name)
        else:
            group_key = ("ticket", str(ticket.id))
        group = groups.setdefault(
            group_key,
            {
                "internal_id": internal_id,
                "external_id": external_id,
                "display_name": display_name,
                "tickets": [],
            },
        )

        redeemed_by = None
        if redeemed.by:
            redeemed_by = {
                "internal_id": str(redeemed.by.id) if redeemed.by.id is not None else None,
                "display_name": redeemed.by.display_name,
            }
        group["tickets"].append(
            {
                "internal_id": str(ticket.id),
                "payment": ticket.payment,
                "purpose": ticket.purpose,
                "redeemed": {
                    "at": redeemed.dt.isoformat() if redeemed.dt else None,
                    "by": redeemed_by,
                    "reason": redeemed.reason,
                },
            }
        )
    return list(groups.values())
