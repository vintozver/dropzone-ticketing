from __future__ import annotations

import base64
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import stripe_secret_key

_STRIPE_API = "https://api.stripe.com/v1"


def _request(
    method: str,
    path: str,
    values: dict[str, object] | None = None,
    *,
    idempotency_key: str | None = None,
):
    key = stripe_secret_key()
    if not key:
        raise ValueError("Online payment is not configured.")
    body = urlencode(values or {}).encode("ascii") if values is not None else None
    authorization = base64.b64encode(f"{key}:".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {authorization}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(
        f"{_STRIPE_API}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.load(error).get("error", {}).get("message")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        raise ValueError(message or "The payment provider rejected the request.") from error


def create_payment_intent(
    *,
    amount: int,
    currency: str,
    email: str,
    event_id: str,
    reservation_token: str,
):
    return _request(
        "POST",
        "/payment_intents",
        {
            "amount": amount,
            "currency": currency.lower(),
            "receipt_email": email,
            "payment_method_types[]": "card",
            "capture_method": "manual",
            "metadata[event_id]": event_id,
            "metadata[reservation_token]": reservation_token,
        },
        idempotency_key=reservation_token,
    )


def retrieve_payment_intent(intent_id: str):
    return _request("GET", f"/payment_intents/{intent_id}")


def capture_payment_intent(intent_id: str):
    return _request("POST", f"/payment_intents/{intent_id}/capture", {})
