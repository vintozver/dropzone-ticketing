from __future__ import annotations

import binascii
import hmac
import secrets
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from time import time
from urllib.parse import parse_qs

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


def _registration_from_cookie(environ: dict) -> tuple[dict[str, object], str] | None:
    payload = _unsign(_cookies(environ).get(AUTHN_CHALLENGE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _CHALLENGE_TTL_SECONDS:
        return None
    state = payload.get("state")
    username = payload.get("username")
    if not isinstance(state, dict) or not isinstance(username, str):
        return None
    return state, username


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
    username = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).get("username", [""])[0].strip()
    registration_options = None
    if username:
        server = _server(environ)
        existing_user = User.objects(id=username).only("fido2_credentials").first()
        credentials = (
            [_credential_data(credential) for credential in existing_user.fido2_credentials]
            if existing_user is not None
            else []
        )
        options, state = server.register_begin(
            PublicKeyCredentialUserEntity(
                id=secrets.token_bytes(16),
                name=username,
                display_name=username,
            ),
            credentials,
            user_verification="discouraged",
        )
        registration_options = _json_options(dict(options))
    status, headers, body = render(
        "register.html",
        rp_id=_rp_id(environ),
        username=username,
        registration_options=registration_options,
    )
    if registration_options is not None:
        payload = {"state": state, "username": username, "issued": time()}
        headers.append(_cookie(AUTHN_CHALLENGE_COOKIE, _signed(payload), max_age=_CHALLENGE_TTL_SECONDS, path="/register"))
    return status, headers, body


def complete_register(environ: dict):
    if not authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Credential registration is disabled.")
    form = read_form(environ)
    username = form.get("username", "").strip()
    if not username:
        return error(HTTPStatus.BAD_REQUEST, "Username is required.")
    registration = _registration_from_cookie(environ)
    if registration is None:
        return error(HTTPStatus.FORBIDDEN, "Registration challenge is missing or expired.")
    state, state_username = registration
    if not hmac.compare_digest(username, state_username):
        return error(HTTPStatus.FORBIDDEN, "Registration username does not match the challenge.")
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
        user = User.objects(id=username).first()
        if user is None:
            user = User(id=username)
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
