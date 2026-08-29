from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from cryptography import x509
from cryptography.hazmat.primitives import serialization
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from ..model.auth import User
from ..model.partner import Partner
from .. import Ticket
from ..time_utils import as_utc
from .config import local_timezone


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
    return partner, payload


def _payload(environ: dict, claims: dict) -> dict:
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
        signed_fields = {name: claims.get(name) for name in ("internal_id", "external_id") if name in claims}
        if signed_fields and any(value.get(name) != item for name, item in signed_fields.items()):
            raise PermissionError("Signed request does not match the request body.")
        if not signed_fields:
            raise PermissionError("The request body must be included in the signed JWT.")
        return value
    return claims


def dispatch(environ: dict):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "")
    try:
        partner, claims = _verify(environ)
        if path in {"/api/report/ticket-redeem/today", "/api/report/ticket-redeem/yesterday"}:
            if method != "GET":
                return _method_not_allowed(["GET"])
            yesterday, today, tomorrow = _day_boundaries()
            start, end = (today, tomorrow) if path.endswith("/today") else (yesterday, today)
            return _json_response(HTTPStatus.OK, _ticket_redeem_report(partner, start=start, end=end))
        if path == "/api/user/list":
            if method != "GET":
                return _method_not_allowed(["GET"])
            users = []
            users_query = User.objects.aggregate(
                [{
                    "$project": {
                        "_id": 1,
                        "display_name": 1,
                        "email": 1,
                        "external_id": {"$ifNull": [f"$partner_uid_map.{partner.id}", None]},
                    }
                }]
            )
            for user in users_query:
                users.append({"external_id": user["external_id"], "internal_id": str(user["_id"]),
                              "display_name": user.get("display_name"), "email": user.get("email")})
            return _json_response(HTTPStatus.OK, users)
        if path == "/api/user":
            if method != "PATCH":
                return _method_not_allowed(["PATCH"])
            values = _payload(environ, claims)
            internal_id, external_id = values.get("internal_id"), values.get("external_id")
            if not isinstance(internal_id, str) or not isinstance(external_id, str):
                raise ValueError("internal_id and external_id are required.")
            try:
                user = User.objects(id=ObjectId(internal_id)).first()
            except InvalidId:
                user = None
            if user is None:
                return _json_response(HTTPStatus.NOT_FOUND, {"error": "User not found."})
            User.objects(id=user.id).update_one(
                **{"set__partner_uid_map__" + str(partner.id): external_id}
            )
            return _json_response(HTTPStatus.OK, {"internal_id": internal_id, "external_id": external_id})
        return _json_response(HTTPStatus.NOT_FOUND, {"error": "Not found."})
    except PermissionError as exc:
        return _json_response(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
    except ValueError as exc:
        return _json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def _method_not_allowed(methods: list[str]):
    return (
        HTTPStatus.METHOD_NOT_ALLOWED,
        [("Content-Type", "application/json; charset=utf-8"), ("Allow", ", ".join(methods))],
        json.dumps({"error": "Method not allowed."}).encode(),
    )


def _day_boundaries(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    display_timezone = local_timezone()
    local_now = as_utc(now).astimezone(display_timezone)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    return as_utc(yesterday), as_utc(today), as_utc(tomorrow)


def _ticket_redeem_report(partner: Partner, *, start: datetime, end: datetime):
    tickets = list(
        Ticket.objects(redeemed__dt__gte=start, redeemed__dt__lt=end)
        .only("id", "issued_to", "payment", "purpose", "redeemed")
        .order_by("redeemed__dt")
    )
    user_ids = {ticket.issued_to.id for ticket in tickets if ticket.issued_to and ticket.issued_to.id is not None}
    users_by_id = {}
    if user_ids:
        users_by_id = {
            str(user.id): user
            for user in User.objects(id__in=user_ids).only("id", "display_name", "partner_uid_map")
        }

    groups = {}
    for ticket in tickets:
        redeemed = ticket.redeemed
        issued_to = ticket.issued_to
        internal_id = str(issued_to.id) if issued_to and issued_to.id is not None else None
        user = users_by_id.get(internal_id) if internal_id else None
        external_id = (user.partner_uid_map or {}).get(str(partner.id)) if user is not None else None
        display_name = (user.display_name if user is not None else None) or (issued_to.display_name if issued_to else None)
        if internal_id is not None:
            group_key = ("id", internal_id)
        elif display_name:
            group_key = ("name", display_name)
        else:
            group_key = ("ticket", str(ticket.id))
        group = groups.setdefault(
            group_key,
            {
                "internal_id": internal_id,
                "external_id": external_id,
                "display_name": display_name,
                "tickets": [],
            },
        )

        redeemed_by = None
        if redeemed.by:
            redeemed_by = {
                "internal_id": str(redeemed.by.id) if redeemed.by.id is not None else None,
                "display_name": redeemed.by.display_name,
            }
        group["tickets"].append(
            {
                "internal_id": str(ticket.id),
                "payment": ticket.payment,
                "purpose": ticket.purpose,
                "redeemed": {
                    "at": redeemed.dt.isoformat() if redeemed.dt else None,
                    "by": redeemed_by,
                    "reason": redeemed.reason,
                },
            }
        )
    return list(groups.values())
