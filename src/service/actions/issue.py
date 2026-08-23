from __future__ import annotations

from http import HTTPStatus

from mongoengine.errors import NotUniqueError
from pymongo.errors import DuplicateKeyError

from ..config import MAX_CODE_ATTEMPTS, MAX_TICKETS


def issue(form: dict[str, str], *, ticket_class, generate_code, render, print_url, issued_user: str | None = None):
    owner = form.get("owner", "").strip()
    payment = form.get("payment", "").strip()
    purpose = form.get("purpose", "").strip()
    if not owner:
        return render("issue.html", HTTPStatus.BAD_REQUEST, error="Owner is required.", owner=owner, payment=payment, purpose=purpose)
    if not payment:
        return render("issue.html", HTTPStatus.BAD_REQUEST, error="Payment is required.", owner=owner, payment=payment, purpose=purpose)
    if not purpose:
        return render("issue.html", HTTPStatus.BAD_REQUEST, error="Purpose is required.", owner=owner, payment=payment, purpose=purpose)

    try:
        count = int(form.get("count", ""))
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_TICKETS:
        return render(
            "issue.html",
            HTTPStatus.BAD_REQUEST,
            error=f"Count must be between 1 and {MAX_TICKETS}.",
            owner=owner,
            payment=payment,
            purpose=purpose,
        )

    tickets = []
    for _ in range(count):
        for _attempt in range(MAX_CODE_ATTEMPTS):
            ticket = ticket_class(
                code=generate_code(),
                owner=owner,
                payment=payment,
                purpose=purpose,
                issued_user=issued_user,
            )
            try:
                ticket.save()
            except (NotUniqueError, DuplicateKeyError):
                continue
            tickets.append(ticket)
            break
        else:
            raise RuntimeError("Could not generate a unique ticket code.")

    return render("issued.html", owner=owner, count=len(tickets), payment=payment, purpose=purpose, print_url=print_url(tickets))
