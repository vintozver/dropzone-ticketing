from __future__ import annotations

import binascii
import hmac
import secrets
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from time import time

from . import auth
from .http import error, read_form
from ..model.auth import Fido2Credential
from fido2.webauthn import (
    AuthenticatorAttestationResponse,
    AttestationObject,
    CollectedClientData,
    RegistrationResponse,
)


def complete_authn(environ: dict):
    state = auth._authn_state_from_cookie(environ)
    if state is None:
        return auth.authentication_error(environ, "Authentication challenge is missing or expired.")
    form = read_form(environ)
    try:
        response = {
            "id": form.get("id", ""),
            "rawId": form.get("rawId", ""),
            "response": {
                "clientDataJSON": form.get("clientDataJSON", ""),
                "authenticatorData": form.get("authenticatorData", ""),
                "signature": form.get("signature", ""),
                "userHandle": form.get("userHandle", None),
            },
            "type": "public-key",
        }
        credential = auth._find_credential(form.get("rawId", ""))
        if credential is None:
            raise ValueError("Unknown credential.")
        server = auth._server(environ)
        server.authenticate_complete(
            state,
            [auth._credential_data(credential)],
            response=response,
        )
        serial = auth._b64encode(credential.id)
    except (binascii.Error, ValueError):
        return auth.authentication_error(
            environ,
            "FIDO2 authentication failed.",
            traceback.format_exc(),
            auth._safe_return_uri(state.get("return_uri")),
        )
    destination = auth._safe_return_uri(state.get("return_uri")) or "/"
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authenticated.")
    headers = [
        ("Location", destination),
        auth._cookie(auth.AUTHN_SESSION_COOKIE, auth._signed({"serial": serial, "issued": time()})),
        auth._clear_cookie(auth.AUTHN_CHALLENGE_COOKIE, path="/authn"),
    ]
    return status, headers, body


def add_credential(environ: dict):
    if auth.authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Authentication is disabled in registration-only mode.")
    user = auth._session_user(environ)
    if user is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication required.")
    register_state = auth._register_state_from_cookie(environ)
    if register_state is None:
        return error(HTTPStatus.FORBIDDEN, "Registration challenge is missing or expired.")
    state, state_user_id = register_state
    if not hmac.compare_digest(str(user.id), state_user_id):
        return error(HTTPStatus.FORBIDDEN, "Registration user does not match the challenge.")
    form = read_form(environ)
    try:
        server = auth._server(environ)
        auth_data = server.register_complete(
            state,
            response=RegistrationResponse(
                id=form.get("id", ""),
                response=AuthenticatorAttestationResponse(
                    client_data=CollectedClientData(auth._b64decode(form.get("clientDataJSON", ""))),
                    attestation_object=AttestationObject(auth._b64decode(form.get("attestationObject", ""))),
                ),
            ),
        )
        credential_id = auth_data.credential_data.credential_id
        if auth._find_credential(auth._b64encode(credential_id)) is not None:
            return error(HTTPStatus.CONFLICT, "FIDO2 credential is already registered.")
        user.fido2_credentials.append(
            Fido2Credential(
                id=credential_id,
                data=bytes(auth_data.credential_data),
                dt=datetime.now(timezone.utc),
                **auth._registration_fields(auth_data),
            )
        )
        user.save()
    except (binascii.Error, ValueError):
        return error(HTTPStatus.FORBIDDEN, "FIDO2 registration failed.", traceback.format_exc())
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Credential registered.")
    headers = [("Location", "/authn"), auth._clear_cookie(auth.AUTHN_CHALLENGE_COOKIE, path="/authn")]
    return status, headers, body


def remove_credential(environ: dict):
    user = auth._session_user(environ)
    if user is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication required.")
    form = read_form(environ)
    token = form.get("csrf", "")
    expected = auth._cookies(environ).get(auth.AUTHN_CSRF_COOKIE, "")
    if not token or not expected or not secrets.compare_digest(token, expected):
        return error(HTTPStatus.FORBIDDEN, "Invalid request.")
    try:
        credential_id = auth._b64decode(form.get("credential_id", ""))
    except (ValueError, binascii.Error):
        return error(HTTPStatus.NOT_FOUND, "FIDO2 credential not found.")
    credentials = user.fido2_credentials
    user.fido2_credentials = [credential for credential in credentials if credential.id != credential_id]
    if len(user.fido2_credentials) == len(credentials):
        return error(HTTPStatus.NOT_FOUND, "FIDO2 credential not found.")
    user.save()
    return HTTPStatus.SEE_OTHER, [("Location", "/authn")], b""
