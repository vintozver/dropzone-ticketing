from __future__ import annotations

from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId


def _user_label(ref) -> str:
    if ref is None:
        return ""
    return ref.display_name or (str(ref.id) if ref.id else "")


def view_ticket(ticket_id: str, viewer, *, ticket_class, render):
    try:
        object_id = ObjectId(ticket_id.strip())
    except (InvalidId, TypeError):
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid ticket identifier.")

    ticket = ticket_class.objects(id=object_id).first()
    if ticket is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Ticket not found.")
    is_admin = "admin" in viewer.get("roles", [])
    if not is_admin and (
        ticket.issued_to is None or ticket.issued_to.id != viewer.get("id")
    ):
        return render("error.html", HTTPStatus.FORBIDDEN, message="Permission denied.")

    redeemed = ticket.redeemed
    return render(
        "ticket.html",
        can_print=is_admin,
        ticket={
            "id": str(ticket.id),
            "code": ticket.code,
            "issued": ticket.issued_utc(),
            "issued_to": _user_label(ticket.issued_to),
            "issued_by": _user_label(ticket.issued_by),
            "purpose": ticket.purpose,
            "payment": ticket.payment,
            "redeemed_at": redeemed.dt if redeemed else None,
            "redeemed_by": _user_label(redeemed.by) if redeemed else "",
            "redeemed_reason": redeemed.reason if redeemed and redeemed.reason else "",
        },
    )
