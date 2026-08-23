from __future__ import annotations

import io
import re
from http import HTTPStatus
from urllib.parse import urlencode

from bson import ObjectId
from bson.errors import InvalidId


def safe_filename(owner: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", owner).strip(".-_")
    return f"tickets-{component or 'owner'}.pdf"


def print_url(tickets) -> str:
    return "/print?" + urlencode([("id", str(ticket.id)) for ticket in tickets])


def print_tickets(ticket_ids, owner: str | None = None, *, ticket_class, pdf_class, render):
    ticket_ids = list(ticket_ids)
    if owner is not None:
        if ticket_ids:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Supply either ticket identifiers or an owner, not both.")
        owner = owner.strip()
        if not owner:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Owner is required.")
        tickets = list(ticket_class.objects(owner=owner, redeemed=None))
    else:
        object_ids = []
        for ticket_id in ticket_ids:
            try:
                object_ids.append(ObjectId(ticket_id.strip()))
            except (InvalidId, TypeError):
                return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid ticket identifier.")
        if not object_ids:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="At least one ticket is required.")
        tickets = list(ticket_class.objects(id__in=object_ids))

    if not tickets:
        return render("error.html", HTTPStatus.NOT_FOUND, message="No tickets found.")

    output = io.BytesIO()
    pdf = pdf_class(output)
    for ticket in tickets:
        pdf.append(ticket)
    pdf.render()
    return HTTPStatus.OK, [("Content-Type", "application/pdf")], output.getvalue()
