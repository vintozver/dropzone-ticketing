from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from cryptography import x509
from cryptography.hazmat.primitives import serialization
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from ...model.partner import Partner

_DT_FORMAT = "%Y%m%dT%H%M%SZ"


def _validate_dt(dt: object, *, now: datetime | None = None) -> None:
    if not isinstance(dt, str):
        raise PermissionError("JWT dt claim is required.")
    try:
        stamped = datetime.strptime(dt, _DT_FORMAT).replace(tzinfo=timezone.utc)
        if len(dt) != 16 or stamped.strftime(_DT_FORMAT) != dt:
            raise ValueError
    except ValueError as exc:
        raise PermissionError("JWT dt claim is invalid.") from exc
    if abs(((now or datetime.now(timezone.utc)) - stamped).total_seconds()) > 60:
        raise PermissionError("JWT dt claim is outside the allowed time window.")


def _json_response(status: HTTPStatus, value: object):
    return status, [("Content-Type", "application/json; charset=utf-8")], json.dumps(value).encode()


def _request_body(environ: dict) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError as exc:
        raise ValueError("Invalid request body length.") from exc
    return environ["wsgi.input"].read(length)


def _verify(environ: dict) -> tuple[Partner, dict]:
    authorization = environ.get("HTTP_AUTHORIZATION", "")
    if not authorization.startswith("Bearer "):
        raise PermissionError("A bearer JWT is required.")
    token = authorization[7:].strip()
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise PermissionError("Invalid JWT.") from exc
    partner_id = header.get("partner")
    key_id = header.get("kid")
    if not isinstance(partner_id, str) or not isinstance(key_id, str):
        raise PermissionError("JWT partner and kid headers are required.")
    try:
        partner = Partner.objects(id=ObjectId(partner_id)).first()
    except InvalidId:
        partner = None
    if partner is None:
        raise PermissionError("Partner does not exist.")
    key = next((item for item in partner.keyset if item.id == key_id), None)
    if key is None:
        raise PermissionError("Unknown partner signing key.")
    key_data = key.pub or key.crt
    if not key_data:
        raise PermissionError("Partner signing key is empty.")
    try:
        public_key = (
            x509.load_der_x509_certificate(bytes(key_data)).public_key()
            if key.crt and not key.pub
            else serialization.load_der_public_key(bytes(key_data))
        )
        algorithm = header.get("alg")
        if algorithm not in {"RS256", "PS256", "ES256", "EdDSA"}:
            raise PermissionError("Unsupported JWT signing algorithm.")
        payload = jwt.decode(token, public_key, algorithms=[algorithm])
    except PermissionError:
        raise
    except ExpiredSignatureError as exc:
        raise PermissionError("JWT has expired.") from exc
    except (ValueError, TypeError, InvalidTokenError) as exc:
        raise PermissionError("Invalid JWT signature.") from exc
    _validate_dt(payload.get("dt"))
    return partner, payload


def _payload(environ: dict, claims: dict, signed_field_names: tuple[str, ...]) -> dict:
    body = claims.get("body")
    if isinstance(body, dict):
        return body
    raw = _request_body(environ)
    if raw:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        missing = [name for name in signed_field_names if name not in claims]
        if missing:
            raise PermissionError("The request body must be included in the signed JWT.")
        signed_fields = {name: claims.get(name) for name in signed_field_names}
        if any(value.get(name) != item for name, item in signed_fields.items()):
            raise PermissionError("Signed request does not match the request body.")
        return value
    return claims


def _method_not_allowed(methods: list[str]):
    return (
        HTTPStatus.METHOD_NOT_ALLOWED,
        [("Content-Type", "application/json; charset=utf-8"), ("Allow", ", ".join(methods))],
        json.dumps({"error": "Method not allowed."}).encode(),
    )
