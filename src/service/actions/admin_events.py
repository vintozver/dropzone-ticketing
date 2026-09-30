from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from mongoengine.errors import NotUniqueError
from mongoengine import Q

from ...model.event import EventHistory
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


def _history(action, user, comment="", *, now):
    return EventHistory(
        dt=now,
        by=_user_ref(user),
        action=action,
        comment=comment.strip() or None,
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
        "dt": dt.astimezone(timezone.utc),
        "duration_minutes": duration,
        "event_type": event_type,
        "price": _price(form.get("price")),
        "currency": currency,
    }


def list_events(*, event_class, render):
    return render("admin_event_list.html", events=event_class.objects().order_by("-dt"))


def new_event(*, event_type_class, render):
    return render(
        "admin_event_form.html",
        event=None,
        event_types=event_type_class.objects(active=True).order_by("name"),
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
            event_types=event_type_class.objects(active=True).order_by("name"),
            values=form,
            error=str(error),
        )
    event = event_class(
        **values,
        history=[_history("created", user, form.get("comment", ""), now=now)],
    )
    event.save()
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/event/view/{event.id}")], b""


def view_event(identifier, *, event_class, event_type_class, render, local_timezone):
    event = _find(event_class, identifier)
    if event is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Event not found.")
    return render(
        "admin_event_view.html",
        event=event,
        event_types=event_type_class.objects().order_by("name"),
        input_dt=event.dt.astimezone(local_timezone).strftime("%Y-%m-%dT%H:%M"),
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
            values = _event_values(
                form,
                event_type_class=event_type_class,
                local_timezone=local_timezone,
            )
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
        updated = event_class.objects(
            Q(reservation_expires_at=None) | Q(reservation_expires_at__lte=now),
            id=event.id,
            customer=None,
        ).modify(
            **{f"set__{name}": value for name, value in values.items()},
            push__history=_history("updated", user, comment, now=now),
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
            push__history=_history("removed", user, comment, now=now),
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
            push__history=_history("restored", user, comment, now=now),
        )
    elif action == "cancel_booking":
        if event.customer is None or event.payment is None:
            return render("error.html", HTTPStatus.CONFLICT, message="This event is not booked.")
        if not comment:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Cancellation comment is required.")
        refund = payment_provider.refund_payment(event.payment.reference)
        if refund.get("status") not in {"pending", "succeeded"}:
            return render("error.html", HTTPStatus.CONFLICT, message="The payment was not refunded.")
        cancelled = event_class.objects(
            id=event.id,
            customer__ne=None,
            payment__reference=event.payment.reference,
        ).modify(
            unset__customer=1,
            unset__payment=1,
            unset__payment_intent=1,
            unset__checkout_customer=1,
            unset__reservation_token=1,
            unset__reservation_expires_at=1,
            push__history=_history("booking_cancelled", user, comment, now=now),
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
        event_types=event_type_class.objects().order_by("name"),
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
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
    elif action == "deactivate":
        event_type.active = False
    elif action == "activate":
        event_type.active = True
    else:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid action.")
    try:
        event_type.save()
    except NotUniqueError:
        return render("error.html", HTTPStatus.CONFLICT, message="That event type already exists.")
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/event-type/view/{event_type.id}")], b""
