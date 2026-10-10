from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, time, timedelta, timezone
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from mongoengine import Q
from mongoengine.errors import NotUniqueError

from ...model.event import Customer, EventHistoryItem, Payment

RESERVATION_MINUTES = 15
MAX_GUEST_HOLDS = 3


def _not_canceling():
    return Q(reservation_canceling=False) | Q(reservation_canceling=None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _window(now: datetime, local_timezone):
    local_today = now.astimezone(local_timezone).date()
    start = datetime.combine(local_today, time.min, local_timezone).astimezone(timezone.utc)
    end = datetime.combine(local_today + timedelta(days=14), time.min, local_timezone).astimezone(timezone.utc)
    return local_today, start, end


def _responses(form: dict[str, str], questions):
    answers = {}
    for question in questions:
        if not question.visible:
            continue
        question_id = str(question.id)
        response_ids = {str(response.id) for response in question.responses}
        if question.multiple:
            selected = [
                response_id
                for response_id in response_ids
                if form.get(f"question_{question_id}_{response_id}") == "on"
            ]
        else:
            selected = [form.get(f"question_{question_id}", "")]
            selected = [response_id for response_id in selected if response_id]
        if any(response_id not in response_ids for response_id in selected):
            raise ValueError("Choose valid responses to the booking questions.")
        if selected:
            answers[question_id] = selected
    return answers


def availability(
    event_type_id: str | None,
    reservation_token: str | None,
    *,
    event_class,
    event_type_class,
    render,
    local_timezone,
    now: datetime,
):
    event_types = list(event_type_class.objects(active=True).order_by("-order", "name"))
    selected_type = None
    if event_type_id:
        try:
            selected_type = event_type_class.objects(id=ObjectId(event_type_id), active=True).first()
        except (InvalidId, TypeError):
            return render(
                "error.html",
                HTTPStatus.BAD_REQUEST,
                message="Choose a valid event type.",
            )

    local_today, start, end = _window(now, local_timezone)
    events = []
    if selected_type is not None:
        events = list(
            event_class.objects(
                event_type=selected_type,
                dt__gte=start,
                dt__lt=end,
                active=True,
            ).order_by("dt")
        )

    held_events = []
    if reservation_token:
        held_events = list(
            event_class.objects(
                _not_canceling(),
                reservation_token=_token_hash(reservation_token),
                reservation_expires_at__gt=now,
                customer=None,
                active=True,
            ).order_by("dt")
        )

    grouped = []
    for offset in range(14):
        day = local_today + timedelta(days=offset)
        day_events = [
            event for event in events if event.dt.astimezone(local_timezone).date() == day
        ]
        available = [
            event
            for event in day_events
            if event.customer is None and not event.is_reserved(now)
        ]
        grouped.append(
            {
                "date": day,
                "events": available,
                "has_events": bool(day_events),
                "sold_out": bool(day_events) and all(event.customer is not None for event in day_events),
            }
        )
    return render(
        "booking.html",
        event_types=event_types,
        selected_type=selected_type,
        days=grouped,
        held_events=held_events,
    )


def hold(
    form: dict[str, str],
    reservation_token: str,
    *,
    event_class,
    render,
    local_timezone,
    now: datetime,
):
    try:
        event_id = ObjectId(form.get("event_id", ""))
    except (InvalidId, TypeError):
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a valid event.")
    _today, start, end = _window(now, local_timezone)
    token_hash = _token_hash(reservation_token)
    event_class.objects(
        reservation_token=token_hash,
        reservation_expires_at__lte=now,
    ).update(
        unset__reservation_token=1,
        unset__reservation_nonce=1,
        unset__reservation_slot=1,
        unset__reservation_expires_at=1,
        unset__checkout_customer=1,
        unset__checkout_responses=1,
        unset__payment_intent=1,
    )
    event = None
    for slot in range(MAX_GUEST_HOLDS):
        try:
            event = (
                event_class.objects(
                    Q(reservation_expires_at=None) | Q(reservation_expires_at__lte=now),
                    id=event_id,
                    dt__gte=start,
                    dt__lt=end,
                    active=True,
                    customer=None,
                )
                .modify(
                    set__reservation_token=token_hash,
                    set__reservation_nonce=secrets.token_urlsafe(32),
                    set__reservation_slot=slot,
                    set__reservation_expires_at=now + timedelta(minutes=RESERVATION_MINUTES),
                    set__reservation_canceling=False,
                    unset__checkout_customer=1,
                    unset__checkout_responses=1,
                    unset__payment_intent=1,
                    new=True,
                )
            )
        except NotUniqueError:
            continue
        if event is None:
            own_hold = event_class.objects(
                _not_canceling(),
                id=event_id,
                reservation_token=token_hash,
                reservation_expires_at__gt=now,
                customer=None,
                active=True,
            ).first()
            if own_hold is not None:
                return (
                    HTTPStatus.SEE_OTHER,
                    [("Location", f"/book/resume?event_id={event_id}")],
                    b"",
                )
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="That event is no longer available. Please choose another time.",
            )
        break
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="You can hold up to three events at a time.",
        )
    return (
        HTTPStatus.SEE_OTHER,
        [("Location", f"/book/resume?event_id={event.id}")],
        b"",
    )


def resume(
    event_id: str | None,
    reservation_token: str | None,
    edit: bool = False,
    *,
    event_class,
    render,
    payment_provider,
    publishable_key: str,
    now: datetime,
):
    try:
        identifier = ObjectId(event_id or "")
    except (InvalidId, TypeError):
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a valid event.")
    if not reservation_token:
        return render(
            "error.html",
            HTTPStatus.NOT_FOUND,
            message="That reservation is not available.",
        )
    event = event_class.objects(
        _not_canceling(),
        id=identifier,
        reservation_token=_token_hash(reservation_token),
        reservation_expires_at__gt=now,
        customer=None,
        active=True,
    ).first()
    if event is None:
        return render(
            "error.html",
            HTTPStatus.NOT_FOUND,
            message="That reservation is not available.",
        )
    if edit or not event.checkout_customer or not event.payment_intent:
        return render(
            "booking_checkout.html",
            event=event,
            step="details",
            customer=event.checkout_customer or {},
            editing=edit,
        )
    if not publishable_key:
        return render(
            "error.html",
            HTTPStatus.SERVICE_UNAVAILABLE,
            message="Online payment is not configured.",
        )
    intent = payment_provider.retrieve_payment_intent(event.payment_intent)
    return render(
        "booking_checkout.html",
        event=event,
        step="payment",
        publishable_key=publishable_key,
        client_secret=intent.get("client_secret"),
        payment_ready=intent.get("status") == "requires_capture",
        payment_intent=event.payment_intent,
        customer=event.checkout_customer,
    )


def contact(
    form: dict[str, str],
    reservation_token: str | None,
    *,
    event_class,
    render,
    payment_provider,
    publishable_key: str,
    now: datetime,
):
    try:
        event_id = ObjectId(form.get("event_id", ""))
    except (InvalidId, TypeError):
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a valid event.")
    event = event_class.objects(
        _not_canceling(),
        id=event_id,
        reservation_token=_token_hash(reservation_token) if reservation_token else "",
        reservation_expires_at__gt=now,
        customer=None,
        active=True,
    ).first()
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="Your 15-minute reservation has expired. Please choose the event again.",
        )

    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    phone = form.get("phone", "").strip()
    editing = form.get("editing") == "1"
    if not name or "@" not in email or email.startswith("@") or email.endswith("@"):
        return render(
            "booking_checkout.html",
            HTTPStatus.BAD_REQUEST,
            event=event,
            step="details",
            error="Enter your name and email address.",
            customer={"name": name, "email": email, "phone": phone},
            editing=editing,
        )
    if not publishable_key:
        return render(
            "error.html",
            HTTPStatus.SERVICE_UNAVAILABLE,
            message="Online payment is not configured.",
        )

    try:
        responses = getattr(event, "checkout_responses", {}) if editing else _responses(
            form,
            event.event_type.questions,
        )
    except ValueError as error:
        return render(
            "booking_checkout.html",
            HTTPStatus.BAD_REQUEST,
            event=event,
            step="details",
            error=str(error),
            customer={"name": name, "email": email, "phone": phone},
            editing=editing,
        )

    customer = Customer(name=name, email=email, phone=phone or None)
    payment_intent = getattr(event, "payment_intent", None)
    try:
        if payment_intent:
            intent = payment_provider.update_payment_intent(payment_intent, email=email)
        else:
            intent = payment_provider.create_payment_intent(
                amount=event.deposit if getattr(event, "deposit", None) is not None else event.price,
                currency=event.currency,
                email=email,
                event_id=str(event.id),
                reservation_token=event.reservation_nonce,
            )
    except ValueError as error:
        return render(
            "error.html",
            HTTPStatus.BAD_GATEWAY,
            message=str(error),
        )
    event = event_class.objects(
        _not_canceling(),
        id=event.id,
        reservation_token=_token_hash(reservation_token),
        reservation_expires_at__gt=now,
        customer=None,
        active=True,
    ).modify(
        set__checkout_customer=customer,
        set__checkout_responses=responses,
        set__payment_intent=intent["id"],
        new=True,
    )
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="Your 15-minute reservation expired before payment could begin.",
        )
    return (
        HTTPStatus.SEE_OTHER,
        [("Location", f"/book/resume?event_id={event.id}")],
        b"",
    )


def cancel(
    form: dict[str, str],
    reservation_token: str | None,
    *,
    event_class,
    render,
    payment_provider,
    now: datetime,
):
    try:
        event_id = ObjectId(form.get("event_id", ""))
    except (InvalidId, TypeError):
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a valid event.")
    event = event_class.objects(
        _not_canceling(),
        id=event_id,
        reservation_token=_token_hash(reservation_token) if reservation_token else "",
        reservation_expires_at__gt=now,
        customer=None,
    ).modify(
        set__reservation_canceling=True,
        new=True,
    )
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="That reservation is no longer available.",
        )
    if event.payment_intent:
        try:
            cancelled_intent = payment_provider.cancel_payment_intent(event.payment_intent)
        except ValueError as error:
            event_class.objects(id=event.id, reservation_canceling=True).update(
                set__reservation_canceling=False,
            )
            return render(
                "error.html",
                HTTPStatus.BAD_GATEWAY,
                message=str(error),
            )
        if cancelled_intent.get("status") != "canceled":
            event_class.objects(id=event.id, reservation_canceling=True).update(
                set__reservation_canceling=False,
            )
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="The payment could not be canceled; the reservation remains in place.",
            )
    released = event_class.objects(
        id=event.id,
        reservation_token=_token_hash(reservation_token),
        reservation_expires_at__gt=now,
        reservation_canceling=True,
        customer=None,
    ).modify(
        unset__reservation_token=1,
        unset__reservation_nonce=1,
        unset__reservation_slot=1,
        unset__reservation_expires_at=1,
        unset__checkout_customer=1,
        unset__checkout_responses=1,
        unset__payment_intent=1,
        set__reservation_canceling=False,
    )
    if released is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="That reservation was already changed.",
        )
    return HTTPStatus.SEE_OTHER, [("Location", "/book")], b""


def complete(
    form: dict[str, str],
    reservation_token: str | None,
    *,
    event_class,
    render,
    payment_provider,
    now: datetime,
):
    intent_id = form.get("payment_intent", "")
    try:
        event_id = ObjectId(form.get("event_id", ""))
    except (InvalidId, TypeError):
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a valid event.")
    event = event_class.objects(
        _not_canceling(),
        id=event_id,
        reservation_token=_token_hash(reservation_token) if reservation_token else "",
        reservation_expires_at__gt=now,
        payment_intent=intent_id,
        customer=None,
        active=True,
    ).first()
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="The reservation has expired or was already completed.",
        )

    intent = payment_provider.retrieve_payment_intent(intent_id)
    amount_due = event.deposit if getattr(event, "deposit", None) is not None else event.price
    expected = (
        intent.get("status") == "requires_capture"
        and intent.get("amount") == amount_due
        and str(intent.get("currency", "")).lower() == event.currency.lower()
        and intent.get("metadata", {}).get("event_id") == str(event.id)
        and intent.get("metadata", {}).get("reservation_token") == event.reservation_nonce
    )
    if not expected:
        return render(
            "error.html",
            HTTPStatus.PAYMENT_REQUIRED,
            message="Payment has not been completed.",
        )

    authorized = event_class.objects(
        _not_canceling(),
        id=event.id,
        reservation_token=_token_hash(reservation_token),
        reservation_expires_at__gt=now,
        payment_intent=intent_id,
        customer=None,
        active=True,
    ).modify(
        set__customer=event.checkout_customer,
        new=True,
    )
    if authorized is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="The event could not be booked.",
        )
    try:
        captured = payment_provider.capture_payment_intent(intent_id)
    except ValueError:
        event_class.objects(id=event.id, payment_intent=intent_id).modify(
            unset__customer=1,
        )
        return render(
            "error.html",
            HTTPStatus.BAD_GATEWAY,
            message="Payment could not be captured. Please try again.",
        )
    if captured.get("status") != "succeeded" or captured.get("amount_received") != amount_due:
        event_class.objects(id=event.id, payment_intent=intent_id).modify(
            unset__customer=1,
        )
        return render(
            "error.html",
            HTTPStatus.PAYMENT_REQUIRED,
            message="Payment could not be captured.",
        )
    payment = Payment(
        provider="stripe",
        reference=intent_id,
        amount=captured["amount_received"],
        currency=captured["currency"].upper(),
        dt=now,
    )
    booked = event_class.objects(
        id=event.id,
        payment_intent=intent_id,
        __raw__={"history.payment.reference": {"$ne": intent_id}},
    ).modify(
        set__responses=event.checkout_responses,
        push_all__history=[
            EventHistoryItem(
                dt=now,
                action="payment",
                payment=payment,
            ),
            EventHistoryItem(dt=now, action="book"),
        ],
        unset__checkout_customer=1,
        unset__checkout_responses=1,
        unset__reservation_token=1,
        unset__reservation_nonce=1,
        unset__reservation_slot=1,
        unset__reservation_expires_at=1,
        new=True,
    )
    if booked is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="Payment succeeded but the booking confirmation could not be recorded.",
        )
    return render(
        "booking_confirmation.html",
        event=booked,
        payment=payment,
    )
