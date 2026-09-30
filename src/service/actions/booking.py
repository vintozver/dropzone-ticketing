from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, time, timedelta, timezone
from http import HTTPStatus

from bson import ObjectId
from mongoengine import Q

from ...model.event import Customer, Payment

RESERVATION_MINUTES = 15


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _window(now: datetime, local_timezone):
    local_today = now.astimezone(local_timezone).date()
    start = datetime.combine(local_today, time.min, local_timezone).astimezone(timezone.utc)
    end = datetime.combine(local_today + timedelta(days=14), time.min, local_timezone).astimezone(timezone.utc)
    return local_today, start, end


def availability(
    event_type_id: str | None,
    *,
    event_class,
    event_type_class,
    render,
    local_timezone,
    now: datetime,
):
    event_types = list(event_type_class.objects(active=True).order_by("name"))
    selected_type = None
    if event_type_id:
        try:
            selected_type = event_type_class.objects(id=ObjectId(event_type_id), active=True).first()
        except Exception:
            selected_type = None

    local_today, start, end = _window(now, local_timezone)
    events = []
    if selected_type is not None:
        events = list(
            event_class.objects(
                event_type=selected_type,
                starts_at__gte=start,
                starts_at__lt=end,
            ).order_by("starts_at")
        )

    grouped = []
    for offset in range(14):
        day = local_today + timedelta(days=offset)
        day_events = [
            event for event in events if event.starts_at.astimezone(local_timezone).date() == day
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
    )


def hold(
    form: dict[str, str],
    *,
    event_class,
    render,
    local_timezone,
    now: datetime,
):
    try:
        event_id = ObjectId(form.get("event_id", ""))
    except Exception:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a valid event.")
    _today, start, end = _window(now, local_timezone)
    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    event = (
        event_class.objects(
            Q(reservation_expires_at=None) | Q(reservation_expires_at__lte=now),
            id=event_id,
            starts_at__gte=start,
            starts_at__lt=end,
            customer=None,
        )
        .modify(
            set__reservation_token=token_hash,
            set__reservation_expires_at=now + timedelta(minutes=RESERVATION_MINUTES),
            unset__checkout_customer=1,
            unset__payment_intent=1,
            new=True,
        )
    )
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="That event is no longer available. Please choose another time.",
        )
    return render("booking_checkout.html", event=event, token=token, step="details")


def contact(
    form: dict[str, str],
    *,
    event_class,
    render,
    payment_provider,
    publishable_key: str,
    now: datetime,
):
    token = form.get("token", "")
    try:
        event_id = ObjectId(form.get("event_id", ""))
    except Exception:
        event_id = None
    event = event_class.objects(
        id=event_id,
        reservation_token=_token_hash(token) if token else "",
        reservation_expires_at__gt=now,
        customer=None,
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
    if not name or not phone or "@" not in email or email.startswith("@") or email.endswith("@"):
        return render(
            "booking_checkout.html",
            HTTPStatus.BAD_REQUEST,
            event=event,
            token=token,
            step="details",
            error="Enter your name, email address, and phone number.",
            customer={"name": name, "email": email, "phone": phone},
        )
    if not publishable_key:
        return render(
            "error.html",
            HTTPStatus.SERVICE_UNAVAILABLE,
            message="Online payment is not configured.",
        )

    customer = Customer(name=name, email=email, phone=phone)
    intent = payment_provider.create_payment_intent(
        amount=event.event_type.price,
        currency=event.event_type.currency,
        email=email,
        event_id=str(event.id),
        reservation_token=_token_hash(token),
    )
    event = event_class.objects(
        id=event.id,
        reservation_token=_token_hash(token),
        reservation_expires_at__gt=now,
        customer=None,
    ).modify(
        set__checkout_customer=customer,
        set__payment_intent=intent["id"],
        new=True,
    )
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="Your 15-minute reservation expired before payment could begin.",
        )
    return render(
        "booking_checkout.html",
        event=event,
        token=token,
        step="payment",
        publishable_key=publishable_key,
        client_secret=intent["client_secret"],
    )


def complete(
    form: dict[str, str],
    *,
    event_class,
    render,
    payment_provider,
    now: datetime,
):
    token = form.get("token", "")
    intent_id = form.get("payment_intent", "")
    try:
        event_id = ObjectId(form.get("event_id", ""))
    except Exception:
        event_id = None
    event = event_class.objects(
        id=event_id,
        reservation_token=_token_hash(token) if token else "",
        reservation_expires_at__gt=now,
        payment_intent=intent_id,
        customer=None,
    ).first()
    if event is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="The reservation has expired or was already completed.",
        )

    intent = payment_provider.retrieve_payment_intent(intent_id)
    expected = (
        intent.get("status") == "requires_capture"
        and intent.get("amount") == event.event_type.price
        and str(intent.get("currency", "")).lower() == event.event_type.currency.lower()
        and intent.get("metadata", {}).get("event_id") == str(event.id)
        and intent.get("metadata", {}).get("reservation_token") == _token_hash(token)
    )
    if not expected:
        return render(
            "error.html",
            HTTPStatus.PAYMENT_REQUIRED,
            message="Payment has not been completed.",
        )

    authorized = event_class.objects(
        id=event.id,
        reservation_token=_token_hash(token),
        reservation_expires_at__gt=now,
        payment_intent=intent_id,
        customer=None,
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
    except Exception:
        event_class.objects(id=event.id, payment_intent=intent_id, payment=None).modify(
            unset__customer=1,
        )
        raise
    if captured.get("status") != "succeeded" or captured.get("amount_received") != event.event_type.price:
        event_class.objects(id=event.id, payment_intent=intent_id, payment=None).modify(
            unset__customer=1,
        )
        return render(
            "error.html",
            HTTPStatus.PAYMENT_REQUIRED,
            message="Payment could not be captured.",
        )
    booked = event_class.objects(
        id=event.id,
        payment_intent=intent_id,
        payment=None,
    ).modify(
        set__payment=Payment(
            provider="stripe",
            reference=intent_id,
            amount=captured["amount_received"],
            currency=captured["currency"].upper(),
            paid_at=now,
        ),
        unset__checkout_customer=1,
        unset__reservation_token=1,
        unset__reservation_expires_at=1,
        new=True,
    )
    if booked is None:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="Payment succeeded but the booking confirmation could not be recorded.",
        )
    return render("booking_confirmation.html", event=booked)
