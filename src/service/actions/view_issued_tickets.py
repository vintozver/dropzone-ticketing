from __future__ import annotations

from datetime import datetime, timezone


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def view_issued_tickets(*, ticket_class, render):
    tickets = []
    for ticket in ticket_class.objects.order_by("-id").limit(500):
        redeemed = ticket.redeemed
        tickets.append(
            {
                "issued": ticket.issued_utc(),
                "owner": ticket.owner,
                "purpose": ticket.purpose,
                "payment": ticket.payment,
                "redeemed_at": _format_datetime(redeemed.dt) if redeemed else "not redeemed",
                "redeemed_by": redeemed.by_user if redeemed and redeemed.by_user else "unknown",
                "redeemed_reason": redeemed.reason if redeemed and redeemed.reason else "unknown",
            }
        )

    return render("report_issued_tickets.html", tickets=tickets)
