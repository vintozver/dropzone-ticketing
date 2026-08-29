from __future__ import annotations

import json
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from cryptography import x509
from cryptography.hazmat.primitives import serialization
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, MissingRequiredClaimError

from ..model.auth import User
from ..model.partner import Partner


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
        payload = jwt.decode(token, public_key, algorithms=[algorithm], options={"require": ["exp"]})
    except PermissionError:
        raise
    except ExpiredSignatureError as exc:
        raise PermissionError("JWT has expired.") from exc
    except MissingRequiredClaimError as exc:
        raise PermissionError("JWT exp claim is missing or invalid.") from exc
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
        if path == "/api/user/list":
            if method != "GET":
                return _method_not_allowed(["GET"])
            users = []
            users_query = User.objects()
            for user in users_query:
                external_id = (user.partner_uid_map or {}).get(str(partner.id))
                users.append({"external_id": external_id, "internal_id": str(user.id),
                              "display_name": user.display_name, "email": user.email})
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
