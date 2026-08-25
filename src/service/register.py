from __future__ import annotations

import binascii
import hmac
import secrets
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
    _credential_data,
    _find_credential,
    _rp_id,
    _server,
    _signed,
    _unsign,
)
from .config import authn_config
from .http import error, read_form, render
from dropzone_ticketing.model.auth import Fido2Credential, User


def _registration_from_cookie(environ: dict) -> tuple[dict[str, object], str, str | None, bool] | None:
    payload = _unsign(_cookies(environ).get(AUTHN_CHALLENGE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _CHALLENGE_TTL_SECONDS:
        return None
    state = payload.get("state")
    username = payload.get("username")
    if not isinstance(state, dict) or not isinstance(username, str):
        return None
    user_id = payload.get("user_id")
    ambiguous_user = payload.get("ambiguous_user") is True
    return state, username, user_id if isinstance(user_id, str) and user_id else None, ambiguous_user


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
    username = query.get("username", [""])[0].strip()
    display_name = query.get("display_name", [""])[0].strip()
    registration_options = None
    if username:
        server = _server(environ)
        matching_users = list(User.objects(display_name=username).only("id", "fido2_credentials").limit(2))
        existing_user = matching_users[0] if len(matching_users) == 1 else None
        credentials = (
            [_credential_data(credential) for credential in existing_user.fido2_credentials]
            if existing_user is not None
            else []
        )
        options, state = server.register_begin(
            PublicKeyCredentialUserEntity(
                id=secrets.token_bytes(16),
                name=username,
                display_name=display_name or username,
            ),
            credentials,
            user_verification="discouraged",
        )
        registration_options = _json_options(dict(options))
    status, headers, body = render(
        "register.html",
        rp_id=_rp_id(environ),
        username=username,
        display_name=display_name,
        registration_options=registration_options,
    )
    if registration_options is not None:
        payload = {"state": state, "username": username, "issued": time()}
        if existing_user is not None:
            payload["user_id"] = str(existing_user.id)
        elif len(matching_users) > 1:
            payload["ambiguous_user"] = True
        headers.append(_cookie(AUTHN_CHALLENGE_COOKIE, _signed(payload), max_age=_CHALLENGE_TTL_SECONDS, path="/register"))
    return status, headers, body


def complete_register(environ: dict):
    if not authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Credential registration is disabled.")
    form = read_form(environ)
    username = form.get("username", "").strip()
    display_name = form.get("display_name", "").strip()
    if not username:
        return error(HTTPStatus.BAD_REQUEST, "Username is required.")
    registration = _registration_from_cookie(environ)
    if registration is None:
        return error(HTTPStatus.FORBIDDEN, "Registration challenge is missing or expired.")
    state, state_username, state_user_id, state_user_ambiguous = registration
    if not hmac.compare_digest(username, state_username):
        return error(HTTPStatus.FORBIDDEN, "Registration username does not match the challenge.")
    if state_user_ambiguous:
        return error(HTTPStatus.CONFLICT, "Registration username is ambiguous.")
    try:
        server = _server(environ)
        auth_data = server.register_complete(
            state,
            response=RegistrationResponse(
                id=form.get("id", ""),
                response=AuthenticatorAttestationResponse(
                    client_data=CollectedClientData(_b64decode(form.get("clientDataJSON", ""))),
                    attestation_object=AttestationObject(_b64decode(form.get("attestationObject", ""))),
                ),
            ),
        )
        credential_data = bytes(auth_data.credential_data)
        credential_id = auth_data.credential_data.credential_id
        if _find_credential(_b64encode(credential_id)) is not None:
            return error(HTTPStatus.CONFLICT, "FIDO2 credential is already registered.")
        user = None
        if state_user_id:
            try:
                user = User.objects(id=ObjectId(state_user_id)).first()
            except (InvalidId, TypeError):
                user = None
            if user is None:
                return error(HTTPStatus.CONFLICT, "Registration user no longer exists.")
        if user is None:
            user = User(display_name=display_name or username or None)
        elif display_name:
            user.display_name = display_name
        user.fido2_credentials.append(
            Fido2Credential(
                id=credential_id,
                data=credential_data,
                dt=datetime.now(timezone.utc),
            )
        )
        user.save()
    except (binascii.Error, ValueError):
        return error(HTTPStatus.FORBIDDEN, "FIDO2 registration failed.", traceback.format_exc())
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Credential registered.")
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_CHALLENGE_COOKIE, path="/register")]
    return status, headers, body
