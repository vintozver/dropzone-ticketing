from __future__ import annotations

from urllib.parse import urlencode

from ...time_utils import format_datetime
from ..config import local_timezone


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
    display_timezone = local_timezone()
    for ticket in ticket_class.objects.order_by("-id").limit(500):
        redeemed = ticket.redeemed
        ticket_url = f"/ticket/{ticket.id}"
        tickets.append(
            {
                "issued": ticket.issued_utc(),
                "url": ticket_url,
                "issued_to": _user_link(ticket.issued_to),
                "issued_by": _user_link(ticket.issued_by),
                "purpose": ticket.purpose,
                "payment": ticket.payment,
                "redeemed_at": format_datetime(redeemed.dt, display_timezone) if redeemed else "",
                "redeemed_by": _user_link(redeemed.by) if redeemed else {"label": "", "url": ""},
                "redeemed_reason": redeemed.reason if redeemed and redeemed.reason else "",
            }
        )

    return render("report_issued_tickets.html", tickets=tickets)
