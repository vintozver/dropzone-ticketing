from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _user_link(ref) -> dict[str, str]:
    if ref is None:
        return {"label": "", "url": ""}
    label = ref.display_name or (str(ref.id) if ref.id else "")
    if ref.id:
        return {"label": label, "url": "/tickets?" + urlencode({"user_id": str(ref.id)})}
    if label:
        return {"label": label, "url": "/tickets?" + urlencode({"display_name": label})}
    return {"label": "", "url": ""}


def view_issued_tickets(*, ticket_class, render):
    tickets = []
    for ticket in ticket_class.objects.order_by("-id").limit(500):
        redeemed = ticket.redeemed
        ticket_url = f"/tickets/{ticket.id}"
        tickets.append(
            {
                "issued": ticket.issued_utc(),
                "url": ticket_url,
                "issued_to": _user_link(ticket.issued_to),
                "issued_by": _user_link(ticket.issued_by),
                "purpose": ticket.purpose,
                "payment": ticket.payment,
                "redeemed_at": _format_datetime(redeemed.dt) if redeemed else "",
                "redeemed_by": _user_link(redeemed.by) if redeemed else {"label": "", "url": ""},
                "redeemed_reason": redeemed.reason if redeemed and redeemed.reason else "",
            }
        )

    return render("report_issued_tickets.html", tickets=tickets)
