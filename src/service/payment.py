from __future__ import annotations

import stripe

from .config import stripe_secret_key


def _client():
    key = stripe_secret_key()
    if not key:
        raise ValueError("Online payment is not configured.")
    return stripe.StripeClient(key)


def _call(method, *args, **kwargs):
    try:
        result = method(*args, **kwargs)
    except stripe.StripeError as error:
        raise ValueError(error.user_message or "The payment provider rejected the request.") from error
    return result.to_dict() if isinstance(result, stripe.StripeObject) else result


def create_payment_intent(
    *,
    amount: int,
    currency: str,
    email: str,
    event_id: str,
    reservation_token: str,
):
    client = _client()
    customer = _call(
        client.v1.customers.create,
        {"email": email},
        {"idempotency_key": f"{reservation_token}-{event_id}-customer"},
    )
    return _call(
        client.v1.payment_intents.create,
        {
            "amount": amount,
            "currency": currency.lower(),
            "receipt_email": email,
            "customer": customer["id"],
            "capture_method": "manual",
            "setup_future_usage": "off_session",
            "metadata": {
                "event_id": event_id,
                "reservation_token": reservation_token,
            },
        },
        {"idempotency_key": f"{reservation_token}-{event_id}"},
    )


def retrieve_payment_intent(intent_id: str):
    return _call(_client().v1.payment_intents.retrieve, intent_id)


def update_payment_intent(intent_id: str, *, email: str):
    client = _client()
    intent = _call(client.v1.payment_intents.retrieve, intent_id)
    customer = intent.get("customer")
    customer_id = customer.get("id") if isinstance(customer, dict) else customer
    if customer_id:
        _call(client.v1.customers.update, customer_id, {"email": email})
    return _call(
        client.v1.payment_intents.update,
        intent_id,
        {"receipt_email": email},
    )


def cancel_payment_intent(intent_id: str):
    return _call(_client().v1.payment_intents.cancel, intent_id)


def capture_payment_intent(intent_id: str):
    return _call(_client().v1.payment_intents.capture, intent_id)


def charge_payment(
    payment_intent_id: str,
    *,
    amount: int,
    currency: str,
    email: str,
    event_id: str,
    idempotency_key: str,
):
    original = retrieve_payment_intent(payment_intent_id)
    payment_method = original.get("payment_method")
    customer = original.get("customer")
    payment_method_id = (
        payment_method.get("id") if isinstance(payment_method, dict) else payment_method
    )
    customer_id = customer.get("id") if isinstance(customer, dict) else customer
    if not payment_method_id or not customer_id:
        raise ValueError("The original payment method is not available.")
    return _call(
        _client().v1.payment_intents.create,
        {
            "amount": amount,
            "currency": currency.lower(),
            "receipt_email": email,
            "customer": customer_id,
            "payment_method": payment_method_id,
            "confirm": True,
            "off_session": True,
            "metadata": {"event_id": event_id},
        },
        {"idempotency_key": idempotency_key},
    )


def refund_payment(
    payment_intent_id: str,
    *,
    amount: int | None = None,
    idempotency_key: str | None = None,
):
    values = {"payment_intent": payment_intent_id}
    if amount is not None:
        values["amount"] = amount
    return _call(
        _client().v1.refunds.create,
        values,
        {"idempotency_key": idempotency_key or f"event-cancellation-{payment_intent_id}"},
    )


def retrieve_refund(refund_id: str):
    return _call(_client().v1.refunds.retrieve, refund_id)
