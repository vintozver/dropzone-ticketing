from __future__ import annotations

from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId

def view_owner_tickets(user_id: str | None, display_name: str | None, *, ticket_class, render):
    user_id = (user_id or "").strip()
    display_name = (display_name or "").strip()
    if bool(user_id) == bool(display_name):
        return render(
            "tickets.html",
            HTTPStatus.BAD_REQUEST,
            registered_owners=[],
            unregistered_owners=[],
            error="Exactly one of user_id or display_name is required.",
        )

    query = {"redeemed": None}
    owner_label = ""
    if user_id:
        try:
            object_id = ObjectId(user_id)
        except (InvalidId, TypeError):
            return render(
                "tickets.html",
                HTTPStatus.BAD_REQUEST,
                registered_owners=[],
                unregistered_owners=[],
                error="Invalid user_id.",
            )
        query["issued_to__id"] = object_id
    else:
        query["issued_to__id"] = None
        query["issued_to__display_name"] = display_name
        owner_label = display_name

    tickets = [
        {
            "id": str(ticket.id),
            "issued": ticket.issued_utc(),
            "issued_by": ticket.issued_by.display_name if ticket.issued_by else None,
            "purpose": ticket.purpose,
            "payment": ticket.payment,
            "issued_to": ticket.issued_to.display_name if ticket.issued_to else None,
        }
        for ticket in ticket_class.objects(**query)
    ]
    if not owner_label and tickets:
        owner_label = tickets[0]["issued_to"] or user_id
    elif not owner_label:
        owner_label = user_id
    return render("tickets_owner.html", owner_label=owner_label, tickets=tickets, user_id=user_id, display_name=display_name)
