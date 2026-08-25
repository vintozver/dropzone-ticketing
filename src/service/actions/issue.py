from __future__ import annotations

from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from mongoengine.errors import NotUniqueError
from pymongo.errors import DuplicateKeyError

from ..config import MAX_CODE_ATTEMPTS, MAX_TICKETS


def issue(
    form: dict[str, str],
    *,
    ticket_class,
    user_class,
    user_ref_class,
    generate_code,
    render,
    print_url,
    issued_by: dict[str, object] | None = None,
):
    user_id = form.get("user_id", "").strip()
    user_display_name = form.get("user_display_name", "").strip()
    payment = form.get("payment", "").strip()
    purpose = form.get("purpose", "").strip()

    if bool(user_id) == bool(user_display_name):
        return render(
            "issue.html",
            HTTPStatus.BAD_REQUEST,
            error="Exactly one of user_id or user_display_name is required.",
            user_id=user_id,
            user_display_name=user_display_name,
            payment=payment,
            purpose=purpose,
        )
    if not payment:
        return render(
            "issue.html",
            HTTPStatus.BAD_REQUEST,
            error="Payment is required.",
            user_id=user_id,
            user_display_name=user_display_name,
            payment=payment,
            purpose=purpose,
        )
    if not purpose:
        return render(
            "issue.html",
            HTTPStatus.BAD_REQUEST,
            error="Purpose is required.",
            user_id=user_id,
            user_display_name=user_display_name,
            payment=payment,
            purpose=purpose,
        )

    if issued_by is None:
        return render(
            "issue.html",
            HTTPStatus.BAD_REQUEST,
            error="Authentication required.",
            user_id=user_id,
            user_display_name=user_display_name,
            payment=payment,
            purpose=purpose,
        )

    try:
        count = int(form.get("count", ""))
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_TICKETS:
        return render(
            "issue.html",
            HTTPStatus.BAD_REQUEST,
            error=f"Count must be between 1 and {MAX_TICKETS}.",
            user_id=user_id,
            user_display_name=user_display_name,
            payment=payment,
            purpose=purpose,
        )

    if user_id:
        try:
            issued_to_user = user_class.objects(id=ObjectId(user_id)).only("id", "login", "display_name").first()
        except (InvalidId, TypeError):
            issued_to_user = None
        if issued_to_user is None:
            return render(
                "issue.html",
                HTTPStatus.BAD_REQUEST,
                error="Selected user does not exist.",
                user_id=user_id,
                user_display_name=user_display_name,
                payment=payment,
                purpose=purpose,
            )
        issued_to = user_ref_class(id=issued_to_user.id, display_name=issued_to_user.display_name or issued_to_user.login)
    else:
        issued_to = user_ref_class(display_name=user_display_name)

    issued_by_ref = user_ref_class(
        id=issued_by.get("id"),
        display_name=str(issued_by.get("display_name", "")).strip() or None,
    )

    tickets = []
    for _ in range(count):
        for _attempt in range(MAX_CODE_ATTEMPTS):
            ticket = ticket_class(
                code=generate_code(),
                issued_to=issued_to,
                payment=payment,
                purpose=purpose,
                issued_by=issued_by_ref,
            )
            try:
                ticket.save()
            except (NotUniqueError, DuplicateKeyError):
                continue
            tickets.append(ticket)
            break
        else:
            raise RuntimeError("Could not generate a unique ticket code.")

    return render(
        "issued.html",
        issued_to_display_name=issued_to.display_name,
        count=len(tickets),
        payment=payment,
        purpose=purpose,
        print_url=print_url(tickets),
    )
