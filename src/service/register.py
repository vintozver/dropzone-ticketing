from __future__ import annotations

import binascii
import hmac
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from time import time
from urllib.parse import parse_qs

from bson import ObjectId
from bson.errors import InvalidId
from fido2.webauthn import (
    AuthenticatorAttestationResponse,
    AttestationObject,
    CollectedClientData,
    PublicKeyCredentialUserEntity,
    RegistrationResponse,
)

from .auth import (
    AUTHN_CHALLENGE_COOKIE,
    _CHALLENGE_TTL_SECONDS,
    _b64decode,
    _b64encode,
    _clear_cookie,
    _cookie,
    _cookies,
    _find_credential,
    _registration_fields,
    _signed,
    _unsign,
)
from . import _fido2
from .config import authn_config
from .http import error, read_form, render
from ..model.auth import Fido2Credential, User

_DEFAULT_REGISTER_DISPLAY_NAME = "User"


def _registration_from_cookie(environ: dict) -> tuple[dict[str, object], str] | None:
    payload = _unsign(_cookies(environ).get(AUTHN_CHALLENGE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _CHALLENGE_TTL_SECONDS:
        return None
    state = payload.get("state")
    user_id = payload.get("user_id")
    if not isinstance(state, dict) or not isinstance(user_id, str) or not user_id:
        return None
    return state, user_id


def _json_options(options: object) -> object:
    if isinstance(options, bytes):
        return _b64encode(options)
    if isinstance(options, dict):
        return {key: _json_options(value) for key, value in options.items()}
    if isinstance(options, (list, tuple)):
        return [_json_options(value) for value in options]
    value = getattr(options, "value", None)
    return value if value is not None else options


def begin_register(environ: dict):
    if not authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Credential registration is disabled.")
    query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    raw_user_id = query.get("user_id", [""])[0].strip()
    user_id = raw_user_id or str(ObjectId())
    display_name = query.get("display_name", [""])[0].strip()
    email = query.get("email", [""])[0].strip().casefold()
    registration_options = None
    if raw_user_id:
        try:
            user_object_id = ObjectId(raw_user_id)
        except (InvalidId, TypeError):
            return error(HTTPStatus.BAD_REQUEST, "User id is invalid.")
        server = _fido2.server(environ)
        existing_user = User.objects(id=user_object_id).only("id", "fido2_credentials").first()
        credentials = (
            [_fido2.credential_data(credential) for credential in existing_user.fido2_credentials]
            if existing_user is not None
            else []
        )
        options, state = server.register_begin(
            PublicKeyCredentialUserEntity(
                id=user_object_id.binary,
                name=raw_user_id,
                display_name=display_name or _DEFAULT_REGISTER_DISPLAY_NAME,
            ),
            credentials,
            user_verification="discouraged",
        )
        registration_options = _json_options(dict(options))
    status, headers, body = render(
        "register.html",
        rp_id=_fido2.rp_id(environ),
        user_id=user_id,
        display_name=display_name,
        email=email,
        registration_options=registration_options,
    )
    if registration_options is not None:
        payload = {"state": state, "user_id": raw_user_id, "issued": time()}
        headers.append(_cookie(AUTHN_CHALLENGE_COOKIE, _signed(payload), max_age=_CHALLENGE_TTL_SECONDS, path="/register"))
    return status, headers, body


def complete_register(environ: dict):
    if not authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Credential registration is disabled.")
    form = read_form(environ)
    user_id = form.get("user_id", "").strip()
    display_name = form.get("display_name", "").strip()
    email = form.get("email", "").strip().casefold()
    if not user_id:
        return error(HTTPStatus.BAD_REQUEST, "User ID is required.")
    if email and ("@" not in email or len(email) > 320):
        return error(HTTPStatus.BAD_REQUEST, "A valid email address is required.")
    registration = _registration_from_cookie(environ)
    if registration is None:
        return error(HTTPStatus.FORBIDDEN, "Registration challenge is missing or expired.")
    state, state_user_id = registration
    if not hmac.compare_digest(user_id, state_user_id):
        return error(HTTPStatus.FORBIDDEN, "Registration user ID does not match the challenge.")
    try:
        user_object_id = ObjectId(user_id)
    except (InvalidId, TypeError):
        return error(HTTPStatus.BAD_REQUEST, "User ID is invalid.")
    try:
        server = _fido2.server(environ)
        attestation_object = _b64decode(form.get("attestationObject", ""))
        auth_data = server.register_complete(
            state,
            response=RegistrationResponse(
                id=form.get("id", ""),
                response=AuthenticatorAttestationResponse(
                    client_data=CollectedClientData(_b64decode(form.get("clientDataJSON", ""))),
                    attestation_object=AttestationObject(attestation_object),
                ),
            ),
        )
        credential_data = bytes(auth_data.credential_data)
        credential_id = auth_data.credential_data.credential_id
        if _find_credential(_b64encode(credential_id)) is not None:
            return error(HTTPStatus.CONFLICT, "FIDO2 credential is already registered.")
        user = User.objects(id=user_object_id).first()
        if user is None:
            user_kwargs = {"id": user_object_id, "display_name": display_name or None}
            if email:
                user_kwargs["email"] = email
            user = User(**user_kwargs)
        elif display_name:
            user.display_name = display_name
        if user.email and email and user.email != email:
            return error(HTTPStatus.BAD_REQUEST, "Email cannot be changed during registration.")
        if not user.email and email:
            user.email = email
        user.fido2_credentials.append(
            Fido2Credential(
                id=credential_id,
                data=credential_data,
                dt=datetime.now(timezone.utc),
                **_registration_fields(auth_data),
            )
        )
        user.save()
    except (binascii.Error, ValueError):
        return error(HTTPStatus.FORBIDDEN, "FIDO2 registration failed.", traceback.format_exc())
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Credential registered.")
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_CHALLENGE_COOKIE, path="/register")]
    return status, headers, body
