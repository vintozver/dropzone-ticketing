from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
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


def _deposit(value, price):
    if value is None or not str(value).strip():
        return None
    deposit = _price(value)
    if deposit < 1 or deposit > price:
        raise ValueError("Deposit must be greater than zero and no more than the price.")
    return deposit


def _event_values(form, *, event_type_class, local_timezone):
    event_type = _find(event_type_class, form.get("event_type", ""))
    if event_type is None:
        raise ValueError("Choose a valid event type.")
    try:
        day = datetime.strptime(form.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Enter a valid event date.") from None
    raw_times = [value.strip() for value in form.get("times", "").splitlines() if value.strip()]
    if not raw_times:
        raise ValueError("Enter at least one event time.")
    try:
        times = [datetime.strptime(value, "%H:%M").time() for value in raw_times]
    except ValueError:
        raise ValueError("Enter event times as HH:MM, one per line.") from None
    if len(set(times)) != len(times):
        raise ValueError("Enter each event time only once.")
    try:
        duration = int(form.get("duration_minutes", ""))
    except ValueError:
        duration = 0
    if duration < 1:
        raise ValueError("Duration must be at least one minute.")
    common_values = {
        "event_type": event_type,
        "duration_minutes": duration,
        **_event_update_values(form),
    }
    return [
        {
            "dt": datetime.combine(day, event_time, tzinfo=local_timezone).astimezone(timezone.utc),
            **common_values,
        }
        for event_time in times
    ]


def _event_update_values(form):
    currency = form.get("currency", "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Currency must be a three-letter code.")
    price = _price(form.get("price"))
    return {
        "price": price,
        "deposit": _deposit(form.get("deposit"), price),
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
        event_values = _event_values(
            form,
            event_type_class=event_type_class,
            local_timezone=local_timezone,
        )
    except ValueError as error:
        return render(
            "admin_event_form.html",
            HTTPStatus.BAD_REQUEST,
            event=None,
            event_types=event_type_class.objects(active=True).order_by("-order", "name"),
            values=form,
            error=str(error),
        )
    events = [
        event_class(
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
        for values in event_values
    ]
    for event in events:
        event.save()
    location = f"/admin/event/view/{events[0].id}" if len(events) == 1 else "/admin/event/list"
    return HTTPStatus.SEE_OTHER, [("Location", location)], b""


def view_event(identifier, *, event_class, event_type_class, render, local_timezone):
    event = _find(event_class, identifier)
    if event is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Event not found.")
    return render(
        "admin_event_view.html",
        event=event,
        balance=_payment_balance(event),
        held=event.is_reserved(datetime.now(timezone.utc)),
    )


def _payment_items(event, action):
    return [
        item.payment
        for item in event.history
        if item.action == action and item.payment is not None
    ]


def _payment_balance(event):
    return sum(payment.amount for payment in _payment_items(event, "payment")) - sum(
        payment.amount for payment in _latest_refunds(event).values()
        if payment.status in {None, "succeeded"}
    )


def _refunded_amount(event, reference):
    return sum(
        payment.amount
        for payment in _latest_refunds(event).values()
        if payment.source_reference == reference
        and payment.status in {None, "succeeded"}
    )


def _latest_refunds(event):
    return {
        payment.reference: payment
        for payment in _payment_items(event, "refund")
    }


def _reconcile_refunds(event, event_class, payment_provider, user, now):
    for payment in _latest_refunds(event).values():
        if payment.status != "pending":
            continue
        refund = payment_provider.retrieve_refund(payment.reference)
        status = refund.get("status")
        if status == "pending":
            continue
        updated = Payment(
            provider=payment.provider,
            reference=payment.reference,
            source_reference=payment.source_reference,
            amount=refund.get("amount", payment.amount),
            currency=str(refund.get("currency", payment.currency)).upper(),
            dt=now,
            status=status or "failed",
        )
        event = event_class.objects(id=event.id).modify(
            push__history=_history(
                "refund",
                user,
                f"Refund status updated to {updated.status}.",
                updated,
                now=now,
            ),
            new=True,
        )
    return event


def _has_pending_refund(event):
    return any(payment.status == "pending" for payment in _latest_refunds(event).values())


def _claim_payment_operation(event, event_class, *, now, complete=False):
    operation = secrets.token_urlsafe(32)
    filters = {
        "id": event.id,
        "customer__ne": None,
    }
    operation_filter = Q(payment_operation=None) | Q(payment_operation_expires_at__lte=now)
    if complete:
        operation_filter &= Q(completed=False) | Q(completed=None)
    claimed = event_class.objects(
        operation_filter,
        **filters,
    ).modify(
        set__payment_operation=operation,
        set__payment_operation_expires_at=now + timedelta(minutes=15),
        new=True,
    )
    return operation, claimed


def _release_payment_operation(event_class, event_id, operation):
    event_class.objects(id=event_id, payment_operation=operation).update(
        unset__payment_operation=1,
        unset__payment_operation_expires_at=1,
    )


def _record_failed_refund(event_class, event_id, operation, user, reference, status, now):
    event_class.objects(id=event_id, payment_operation=operation).modify(
        push__history=_history(
            "comment",
            user,
            f"Refund attempt for {reference} failed with status {status or 'unknown'}.",
            now=now,
        ),
        unset__payment_operation=1,
        unset__payment_operation_expires_at=1,
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
    elif action == "cancel_hold":
        if event.customer is not None or not event.is_reserved(now):
            return render("error.html", HTTPStatus.CONFLICT, message="This event has no active hold.")
        claimed = event_class.objects(
            Q(reservation_canceling=False) | Q(reservation_canceling=None),
            id=event.id,
            customer=None,
            reservation_token=event.reservation_token,
            reservation_nonce=event.reservation_nonce,
            payment_intent=event.payment_intent,
            reservation_expires_at__gt=now,
        ).modify(
            set__reservation_canceling=True,
            new=True,
        )
        if claimed is None:
            return render("error.html", HTTPStatus.CONFLICT, message="The hold was already changed.")
        event = claimed
        if event.payment_intent:
            try:
                cancelled_intent = payment_provider.cancel_payment_intent(event.payment_intent)
            except ValueError as error:
                event_class.objects(id=event.id, reservation_canceling=True).update(
                    set__reservation_canceling=False,
                )
                return render("error.html", HTTPStatus.BAD_GATEWAY, message=str(error))
            if cancelled_intent.get("status") != "canceled":
                event_class.objects(id=event.id, reservation_canceling=True).update(
                    set__reservation_canceling=False,
                )
                return render(
                    "error.html",
                    HTTPStatus.CONFLICT,
                    message="The payment could not be canceled; the hold remains in place.",
                )
        cancelled = event_class.objects(
            id=event.id,
            customer=None,
            reservation_token=event.reservation_token,
            reservation_nonce=event.reservation_nonce,
            payment_intent=event.payment_intent,
            reservation_expires_at__gt=now,
            reservation_canceling=True,
        ).modify(
            unset__reservation_token=1,
            unset__reservation_nonce=1,
            unset__reservation_slot=1,
            unset__reservation_expires_at=1,
            unset__checkout_customer=1,
            unset__checkout_responses=1,
            unset__payment_intent=1,
            set__reservation_canceling=False,
            push__history=_history("cancel", user, comment or "Hold canceled.", now=now),
        )
        if cancelled is None:
            return render("error.html", HTTPStatus.CONFLICT, message="The hold was already changed.")
    elif action in {"charge", "complete"}:
        if event.customer is None:
            return render("error.html", HTTPStatus.CONFLICT, message="This event is not booked.")
        if action == "complete":
            if getattr(event, "completed", False):
                return render("error.html", HTTPStatus.CONFLICT, message="This event is already complete.")
        else:
            try:
                amount = _price(form.get("amount"))
            except ValueError as error:
                return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
            if amount < 1:
                return render("error.html", HTTPStatus.BAD_REQUEST, message="Charge must be greater than zero.")
        try:
            event = _reconcile_refunds(event, event_class, payment_provider, user, now)
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_GATEWAY, message=str(error))
        if _has_pending_refund(event):
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="A refund is still pending.",
            )
        operation, claimed = _claim_payment_operation(
            event,
            event_class,
            now=now,
            complete=action == "complete",
        )
        if claimed is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="Another payment operation is already in progress.",
            )
        event = claimed
        payments = _payment_items(event, "payment")
        if not payments:
            _release_payment_operation(event_class, event.id, operation)
            return render("error.html", HTTPStatus.CONFLICT, message="No payment method is available.")
        if action == "complete":
            amount = max(event.price - _payment_balance(event), 0)
        history = []
        charged_reference = None
        if amount:
            try:
                charged = payment_provider.charge_payment(
                    payments[-1].reference,
                    amount=amount,
                    currency=event.currency,
                    email=event.customer.email,
                    event_id=str(event.id),
                    idempotency_key=f"event-{event.id}-{action}-{len(event.history)}",
                )
            except ValueError as error:
                _release_payment_operation(event_class, event.id, operation)
                return render("error.html", HTTPStatus.BAD_GATEWAY, message=str(error))
            if charged.get("status") != "succeeded" or charged.get("amount_received") != amount:
                _release_payment_operation(event_class, event.id, operation)
                return render(
                    "error.html",
                    HTTPStatus.PAYMENT_REQUIRED,
                    message="The charge was not completed.",
                )
            history.append(
                _history(
                    "payment",
                    user,
                    payment=Payment(
                        provider="stripe",
                        reference=charged["id"],
                        amount=amount,
                        currency=str(charged["currency"]).upper(),
                        dt=now,
                    ),
                    now=now,
                )
            )
            charged_reference = charged["id"]
        if action == "complete":
            history.append(_history("comment", user, comment or "Event completed.", now=now))
        filters = {"id": event.id, "payment_operation": operation}
        if charged_reference:
            filters["__raw__"] = {"history.payment.reference": {"$ne": charged_reference}}
        recorded = event_class.objects(**filters).modify(
            set__completed=True if action == "complete" else getattr(event, "completed", False),
            push_all__history=history,
            unset__payment_operation=1,
            unset__payment_operation_expires_at=1,
            new=True,
        )
        if recorded is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="The charge succeeded but the event was already changed.",
            )
    elif action == "refund":
        reference = form.get("reference", "")
        try:
            amount = _price(form.get("amount"))
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
        try:
            event = _reconcile_refunds(event, event_class, payment_provider, user, now)
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_GATEWAY, message=str(error))
        if _has_pending_refund(event):
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="A refund is still pending.",
            )
        operation, claimed = _claim_payment_operation(event, event_class, now=now)
        if claimed is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="Another payment operation is already in progress.",
            )
        event = claimed
        payment = next(
            (item for item in _payment_items(event, "payment") if item.reference == reference),
            None,
        )
        if payment is None:
            _release_payment_operation(event_class, event.id, operation)
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a valid payment.")
        available = payment.amount - _refunded_amount(event, reference)
        if amount < 1 or amount > available:
            _release_payment_operation(event_class, event.id, operation)
            return render(
                "error.html",
                HTTPStatus.BAD_REQUEST,
                message="Refund must be greater than zero and no more than the refundable amount.",
            )
        try:
            refund = payment_provider.refund_payment(
                reference,
                amount=amount,
                idempotency_key=f"event-{event.id}-refund-{reference}-{amount}-{len(event.history)}",
            )
        except ValueError as error:
            _release_payment_operation(event_class, event.id, operation)
            return render("error.html", HTTPStatus.BAD_GATEWAY, message=str(error))
        if refund.get("status") not in {"succeeded", "pending"}:
            _record_failed_refund(
                event_class,
                event.id,
                operation,
                user,
                reference,
                refund.get("status"),
                now,
            )
            return render("error.html", HTTPStatus.CONFLICT, message="The refund was not completed.")
        recorded = event_class.objects(
            id=event.id,
            payment_operation=operation,
            __raw__={"history.payment.reference": {"$ne": refund["id"]}},
        ).modify(
            push__history=_history(
                "refund",
                user,
                comment,
                Payment(
                    provider="stripe",
                    reference=refund["id"],
                    source_reference=reference,
                    amount=amount,
                    currency=str(refund.get("currency", payment.currency)).upper(),
                    dt=now,
                    status=refund.get("status"),
                ),
                now=now,
            ),
            unset__payment_operation=1,
            unset__payment_operation_expires_at=1,
            new=True,
        )
        if recorded is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="The refund succeeded but the event was already changed.",
            )
    elif action == "cancel_booking":
        if event.customer is None or not _payment_items(event, "payment"):
            return render("error.html", HTTPStatus.CONFLICT, message="This event is not booked.")
        if not comment:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Cancellation comment is required.")
        try:
            event = _reconcile_refunds(event, event_class, payment_provider, user, now)
        except ValueError as error:
            return render("error.html", HTTPStatus.BAD_GATEWAY, message=str(error))
        if _has_pending_refund(event):
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="A refund is still pending; the booking remains in place.",
            )
        operation, claimed = _claim_payment_operation(event, event_class, now=now)
        if claimed is None:
            return render(
                "error.html",
                HTTPStatus.CONFLICT,
                message="Another payment operation is already in progress.",
            )
        event = claimed
        payments = _payment_items(event, "payment")
        for payment in payments:
            amount = payment.amount - _refunded_amount(event, payment.reference)
            if amount < 1:
                continue
            try:
                refund = payment_provider.refund_payment(
                    payment.reference,
                    amount=amount,
                    idempotency_key=(
                        f"event-{event.id}-cancel-{payment.reference}-{amount}-{len(event.history)}"
                    ),
                )
            except ValueError as error:
                _release_payment_operation(event_class, event.id, operation)
                return render("error.html", HTTPStatus.BAD_GATEWAY, message=str(error))
            if refund.get("status") not in {"succeeded", "pending"}:
                _record_failed_refund(
                    event_class,
                    event.id,
                    operation,
                    user,
                    payment.reference,
                    refund.get("status"),
                    now,
                )
                return render(
                    "error.html",
                    HTTPStatus.CONFLICT,
                    message="The refund is not complete; the booking remains in place.",
                )
            refund_history = _history(
                "refund",
                user,
                payment=Payment(
                    provider="stripe",
                    reference=refund["id"],
                    source_reference=payment.reference,
                    amount=amount,
                    currency=str(refund.get("currency", payment.currency)).upper(),
                    dt=now,
                    status=refund.get("status"),
                ),
                now=now,
            )
            recorded = event_class.objects(
                id=event.id,
                payment_operation=operation,
                __raw__={"history.payment.reference": {"$ne": refund["id"]}},
            ).modify(
                push__history=refund_history,
                new=True,
            )
            if recorded is None:
                return render(
                    "error.html",
                    HTTPStatus.CONFLICT,
                    message="The refund succeeded but the event was already changed.",
                )
            event = recorded
            if refund.get("status") == "pending":
                _release_payment_operation(event_class, event.id, operation)
                return render(
                    "error.html",
                    HTTPStatus.CONFLICT,
                    message="The refund is pending; the booking remains in place.",
                )
        cancelled = event_class.objects(
            id=event.id,
            payment_operation=operation,
        ).modify(
            unset__customer=1,
            set__completed=False,
            unset__payment_intent=1,
            unset__checkout_customer=1,
            unset__checkout_responses=1,
            unset__responses=1,
            unset__reservation_token=1,
            unset__reservation_nonce=1,
            unset__reservation_slot=1,
            unset__reservation_expires_at=1,
            unset__payment_operation=1,
            unset__payment_operation_expires_at=1,
            push__history=_history("cancel", user, comment, now=now),
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
        price = _price(form.get("price"))
        event_type = event_type_class(
            name=name,
            description=form.get("description", "").strip() or None,
            price=price,
            deposit=_deposit(form.get("deposit"), price),
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
            event_type.deposit = _deposit(form.get("deposit"), event_type.price)
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
