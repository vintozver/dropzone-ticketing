from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from mongoengine.errors import NotUniqueError
from mongoengine import Q

from ...model.event import EventHistoryItem, Payment, Question, QuestionResponse
from ...model.ticket import UserRef


def _find(document_class, identifier):
    try:
        object_id = ObjectId(identifier)
    except (InvalidId, TypeError):
        return None
    return document_class.objects(id=object_id).first()


def _user_ref(user):
    return UserRef(
        id=user.get("id"),
        display_name=str(user.get("display_name", "")).strip() or None,
    )


def _history(action, user=None, comment="", payment=None, *, now):
    return EventHistoryItem(
        dt=now,
        by=_user_ref(user) if user is not None else None,
        action=action,
        comment=comment.strip() or None,
        payment=payment,
    )


def _price(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid price.") from None
    if amount < 0 or amount.as_tuple().exponent < -2:
        raise ValueError("Enter a non-negative price with at most two decimal places.")
    return int(amount * 100)


def _event_values(form, *, event_type_class, local_timezone):
    event_type = _find(event_type_class, form.get("event_type", ""))
    if event_type is None:
        raise ValueError("Choose a valid event type.")
    try:
        dt = datetime.fromisoformat(form.get("dt", ""))
    except ValueError:
        raise ValueError("Enter a valid event date and time.") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_timezone)
    return {
        "dt": dt.astimezone(timezone.utc),
        "event_type": event_type,
        **_event_update_values(form),
    }


def _event_update_values(form):
    try:
        duration = int(form.get("duration_minutes", ""))
    except ValueError:
        duration = 0
    if duration < 1:
        raise ValueError("Duration must be at least one minute.")
    currency = form.get("currency", "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Currency must be a three-letter code.")
    return {
        "duration_minutes": duration,
        "price": _price(form.get("price")),
        "currency": currency,
    }


def _order(value, label):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} order must be an integer.") from None


def list_events(*, event_class, render):
    return render("admin_event_list.html", events=event_class.objects().order_by("-dt"))


def new_event(*, event_type_class, render):
    return render(
        "admin_event_form.html",
        event=None,
        event_types=event_type_class.objects(active=True).order_by("-order", "name"),
        values={},
    )


def create_event(
    form,
    user,
    *,
    event_class,
    event_type_class,
    render,
    local_timezone,
    now,
):
    try:
        values = _event_values(form, event_type_class=event_type_class, local_timezone=local_timezone)
    except ValueError as error:
        return render(
            "admin_event_form.html",
            HTTPStatus.BAD_REQUEST,
            event=None,
            event_types=event_type_class.objects(active=True).order_by("-order", "name"),
            values=form,
            error=str(error),
        )
    event = event_class(
        **values,
        history=[
            _history(
                "comment",
                user,
                form.get("comment", "").strip() or "Event created.",
                now=now,
            )
        ],
    )
    event.save()
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/event/view/{event.id}")], b""


def view_event(identifier, *, event_class, event_type_class, render, local_timezone):
    event = _find(event_class, identifier)
    if event is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Event not found.")
    payment_item = next(
        (
            item
            for item in reversed(event.history)
            if item.action == "payment" and item.payment is not None
        ),
        None,
    )
    return render(
        "admin_event_view.html",
        event=event,
        payment=payment_item.payment if payment_item else None,
    )


def update_event(
    identifier,
    form,
    user,
    *,
    event_class,
    event_type_class,
    payment_provider,
    render,
    local_timezone,
    now,
):
    event = _find(event_class, identifier)
    if event is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Event not found.")
    action = form.get("action", "")
    comment = form.get("comment", "").strip()

    if action == "comment":
        if not comment:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Comment is required.")
        event_class.objects(id=event.id).modify(
            push__history=_history("comment", user, comment, now=now),
        )
    elif action == "update":
        try:
            values = _event_update_values(form)
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
        updated = event_class.objects(
            Q(reservation_expires_at=None) | Q(reservation_expires_at__lte=now),
            id=event.id,
            customer=None,
        ).modify(
            **{f"set__{name}": value for name, value in values.items()},
            push__history=_history(
                "comment",
                user,
                comment or "Event details updated.",
                now=now,
            ),
            new=True,
        )
        if updated is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="Booked or reserved events cannot be changed.",
            )
    elif action == "remove":
        removed = event_class.objects(
            Q(reservation_expires_at=None) | Q(reservation_expires_at__lte=now),
            id=event.id,
            customer=None,
        ).modify(
            set__active=False,
            push__history=_history(
                "comment",
                user,
                comment or "Event removed.",
                now=now,
            ),
            new=True,
        )
        if removed is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="Cancel the booking or wait for checkout to expire before removing this event.",
            )
    elif action == "restore":
        event_class.objects(id=event.id).modify(
            set__active=True,
            push__history=_history(
                "comment",
                user,
                comment or "Event restored.",
                now=now,
            ),
        )
    elif action == "cancel_booking":
        payment_item = next(
            (
                item
                for item in reversed(event.history)
                if item.action == "payment" and item.payment is not None
            ),
            None,
        )
        if event.customer is None or payment_item is None:
            return render("error.html", HTTPStatus.CONFLICT, message="This event is not booked.")
        if not comment:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Cancellation comment is required.")
        refund = payment_provider.refund_payment(payment_item.payment.reference)
        if refund.get("status") != "succeeded":
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="The refund is not complete; the booking remains in place.",
            )
        cancelled = event_class.objects(
            id=event.id,
            customer__ne=None,
            history__payment__reference=payment_item.payment.reference,
        ).modify(
            unset__customer=1,
            unset__payment_intent=1,
            unset__checkout_customer=1,
            unset__checkout_responses=1,
            unset__responses=1,
            unset__reservation_token=1,
            unset__reservation_nonce=1,
            unset__reservation_slot=1,
            unset__reservation_expires_at=1,
            push_all__history=[
                _history(
                    "refund",
                    user,
                    payment=Payment(
                        provider="stripe",
                        reference=refund["id"],
                        amount=refund.get("amount", payment_item.payment.amount),
                        currency=str(
                            refund.get("currency", payment_item.payment.currency)
                        ).upper(),
                        dt=now,
                    ),
                    now=now,
                ),
                _history("cancel", user, comment, now=now),
            ],
            new=True,
        )
        if cancelled is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="Payment was refunded but the booking was already changed.",
            )
        return HTTPStatus.SEE_OTHER, [("Location", f"/admin/event/view/{event.id}")], b""
    else:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid action.")
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/event/view/{event.id}")], b""


def list_event_types(*, event_type_class, render):
    return render(
        "admin_event_type_list.html",
        event_types=event_type_class.objects().order_by("-order", "name"),
    )


def create_event_type(form, *, event_type_class, render):
    name = form.get("name", "").strip()
    currency = form.get("currency", "").strip().upper()
    if not name:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Name is required.")
    if len(currency) != 3 or not currency.isalpha():
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Currency must be a three-letter code.")
    try:
        event_type = event_type_class(
            name=name,
            description=form.get("description", "").strip() or None,
            price=_price(form.get("price")),
            currency=currency,
            order=_order(form.get("order", "0"), "Event type"),
        )
        event_type.save()
    except NotUniqueError:
        return render("error.html", HTTPStatus.CONFLICT, message="That event type already exists.")
    except ValueError as error:
        return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/event-type/view/{event_type.id}")], b""


def view_event_type(identifier, *, event_type_class, render):
    event_type = _find(event_type_class, identifier)
    if event_type is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Event type not found.")
    return render("admin_event_type_view.html", event_type=event_type)


def update_event_type(identifier, form, *, event_type_class, render):
    event_type = _find(event_type_class, identifier)
    if event_type is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Event type not found.")
    action = form.get("action", "")
    if action == "update":
        name = form.get("name", "").strip()
        currency = form.get("currency", "").strip().upper()
        if not name or len(currency) != 3 or not currency.isalpha():
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Enter a name and currency.")
        try:
            event_type.name = name
            event_type.description = form.get("description", "").strip() or None
            event_type.price = _price(form.get("price"))
            event_type.currency = currency
            event_type.order = _order(form.get("order", "0"), "Event type")
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
    elif action == "deactivate":
        event_type.active = False
    elif action == "activate":
        event_type.active = True
    elif action == "add_question":
        text = form.get("question", "").strip()
        response_labels = [
            line.strip()
            for line in form.get("responses", "").splitlines()
            if line.strip()
        ]
        try:
            order = int(form.get("order", "0"))
        except ValueError:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Question order must be an integer.")
        if not text or not response_labels:
            return render(
                "error.html",
                HTTPStatus.BAD_REQUEST,
                message="Question text and at least one response are required.",
            )
        event_type.questions.append(
            Question(
                text=text,
                order=order,
                multiple=form.get("multiple") == "on",
                visible=form.get("visible") == "on",
                responses=[
                    QuestionResponse(label=label)
                    for label in response_labels
                ],
            )
        )
    elif action == "remove_question":
        try:
            question_id = ObjectId(form.get("question_id", ""))
        except (InvalidId, TypeError):
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid question.")
        event_type.questions = [
            question for question in event_type.questions if question.id != question_id
        ]
    elif action == "update_question_order":
        try:
            question_id = ObjectId(form.get("question_id", ""))
        except (InvalidId, TypeError):
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid question.")
        question = next(
            (question for question in event_type.questions if question.id == question_id),
            None,
        )
        if question is None:
            return render("error.html", HTTPStatus.NOT_FOUND, message="Question not found.")
        try:
            question.order = _order(form.get("order"), "Question")
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
    else:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid action.")
    try:
        event_type.save()
    except NotUniqueError:
        return render("error.html", HTTPStatus.CONFLICT, message="That event type already exists.")
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/event-type/view/{event_type.id}")], b""
