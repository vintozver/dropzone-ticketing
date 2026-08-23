from __future__ import annotations

from http import HTTPStatus


def view_owner_tickets(owner: str, *, ticket_class, render):
    owner = owner.strip()
    if not owner:
        return render("tickets.html", HTTPStatus.BAD_REQUEST, owners=[], error="Owner is required.")

    tickets = [
        {
            "id": str(ticket.id),
            "issued": ticket.issued_utc(),
            "issued_user": ticket.issued_user,
            "purpose": ticket.purpose,
            "payment": ticket.payment,
        }
        for ticket in ticket_class.objects(owner=owner, redeemed=None)
    ]
    return render("tickets_owner.html", owner=owner, tickets=tickets)
