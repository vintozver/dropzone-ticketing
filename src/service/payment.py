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
    converter = getattr(result, "to_dict_recursive", None)
    return converter() if callable(converter) else result


def create_payment_intent(
    *,
    amount: int,
    currency: str,
    email: str,
    event_id: str,
    reservation_token: str,
):
    client = _client()
    return _call(
        client.v1.payment_intents.create,
        {
            "amount": amount,
            "currency": currency.lower(),
            "receipt_email": email,
            "payment_method_types": ["card"],
            "capture_method": "manual",
            "metadata": {
                "event_id": event_id,
                "reservation_token": reservation_token,
            },
        },
        {"idempotency_key": reservation_token},
    )


def retrieve_payment_intent(intent_id: str):
    return _call(_client().v1.payment_intents.retrieve, intent_id)


def capture_payment_intent(intent_id: str):
    return _call(_client().v1.payment_intents.capture, intent_id)


def refund_payment(payment_intent_id: str):
    return _call(
        _client().v1.refunds.create,
        {"payment_intent": payment_intent_id},
        {"idempotency_key": f"event-cancellation-{payment_intent_id}"},
    )
