from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import traceback
from datetime import datetime, timezone
from hashlib import sha256
from http import HTTPStatus
from time import time
from urllib.parse import quote

from fido2.server import Fido2Server
from fido2.webauthn import (
    AuthenticatorAttestationResponse,
    AttestationObject,
    CollectedClientData,
    PublicKeyCredentialUserEntity,
    PublicKeyCredentialRpEntity,
    RegistrationResponse,
)

from .config import authn_config, session_secret
from .http import error, read_form, render
from dropzone_ticketing.model.auth import Fido2Credential, User

AUTHN_CHALLENGE_COOKIE = "authn_challenge"
AUTHN_SESSION_COOKIE = "authn_session"
_CHALLENGE_TTL_SECONDS = 300
_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _signature(payload: str) -> str:
    return _b64encode(hmac.new(session_secret(), payload.encode("ascii"), sha256).digest())


def _signed(payload: dict[str, object]) -> str:
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_signature(encoded)}"


def _unsign(value: str) -> dict[str, object] | None:
    try:
        encoded, signature = value.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _signature(encoded)):
        return None
    try:
        return json.loads(_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _cookies(environ: dict) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for item in environ.get("HTTP_COOKIE", "").split(";"):
        if "=" in item:
            name, value = item.strip().split("=", 1)
            cookies[name] = value
    return cookies


def _cookie(
    name: str,
    value: str,
    *,
    max_age: int = _COOKIE_MAX_AGE_SECONDS,
    path: str = "/",
    same_site: str = "Lax",
) -> tuple[str, str]:
    return (
        "Set-Cookie",
        f"{name}={quote(value, safe='')}; Max-Age={max_age}; Path={path}; Secure; HttpOnly; SameSite={same_site}",
    )


def _clear_cookie(name: str, *, path: str = "/") -> tuple[str, str]:
    return ("Set-Cookie", f"{name}=; Max-Age=0; Path={path}; Secure; HttpOnly; SameSite=Strict")


def _request_host(environ: dict) -> str:
    return environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "localhost"


def _rp_id(environ: dict) -> str:
    return _request_host(environ).split(":", 1)[0]


def _origin(environ: dict) -> str:
    scheme = environ.get("wsgi.url_scheme") or "http"
    return f"{scheme}://{_request_host(environ)}"


def _json_options(options: object) -> object:
    if isinstance(options, bytes):
        return _b64encode(options)
    if isinstance(options, dict):
        return {key: _json_options(value) for key, value in options.items()}
    if isinstance(options, (list, tuple)):
        return [_json_options(value) for value in options]
    value = getattr(options, "value", None)
    return value if value is not None else options


def _authn_state_from_cookie(environ: dict) -> dict[str, object] | None:
    payload = _unsign(_cookies(environ).get(AUTHN_CHALLENGE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _CHALLENGE_TTL_SECONDS:
        return None
    state = payload.get("state")
    if isinstance(state, dict):
        return state
    challenge = payload.get("challenge")
    if not isinstance(challenge, str):
        return None
    return {"challenge": challenge, "user_verification": None}


def _register_state_from_cookie(environ: dict) -> tuple[dict[str, object], str] | None:
    payload = _unsign(_cookies(environ).get(AUTHN_CHALLENGE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _CHALLENGE_TTL_SECONDS:
        return None
    state = payload.get("register_state")
    user_id = payload.get("register_user")
    if not isinstance(state, dict) or not isinstance(user_id, str):
        return None
    return state, user_id


def _is_authenticated(environ: dict) -> bool:
    payload = _unsign(_cookies(environ).get(AUTHN_SESSION_COOKIE, ""))
    if not payload:
        return False
    serial = payload.get("serial")
    issued = float(payload.get("issued", 0))
    return (
        (isinstance(serial, str) and _credential_owner(serial) is not None)
        or (isinstance(payload.get("user_id"), str) and User.objects(id=payload["user_id"]).first() is not None)
    ) and time() - issued <= _COOKIE_MAX_AGE_SECONDS


def _credential_owner(encoded_id: str) -> User | None:
    try:
        credential_id = _b64decode(encoded_id)
    except (ValueError, binascii.Error):
        return None
    user = User.objects(fido2_credentials__id=credential_id).first()
    return user


def _find_credential(encoded_id: str) -> Fido2Credential | None:
    try:
        credential_id = _b64decode(encoded_id)
    except (ValueError, binascii.Error):
        return None
    user = _credential_owner(encoded_id)
    if user is None:
        return None
    return next(
        (credential for credential in user.fido2_credentials if credential.id == credential_id),
        None,
    )


def _session_user(environ: dict) -> User | None:
    payload = _unsign(_cookies(environ).get(AUTHN_SESSION_COOKIE, ""))
    if not payload:
        return None
    serial = payload.get("serial")
    issued = float(payload.get("issued", 0))
    if time() - issued > _COOKIE_MAX_AGE_SECONDS:
        return None
    user_id = payload.get("user_id")
    if isinstance(user_id, str):
        return User.objects(id=user_id).first()
    if not isinstance(serial, str):
        return None
    return _credential_owner(serial)


def current_user_id(environ: dict) -> str | None:
    user = _session_user(environ)
    if user is None:
        return None
    return user.id


def _credential_display_id(credential: Fido2Credential) -> str:
    hex_value = credential.id.hex()
    if len(hex_value) <= 16:
        return hex_value
    return f"{hex_value[:8]}…{hex_value[-8:]}"


def begin_authn(environ: dict):
    if authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Authentication is disabled in registration-only mode.")
    challenge = secrets.token_bytes(32)
    server = _server(environ)
    credentials = [
        credential
        for user in User.objects().only("fido2_credentials")
        for credential in user.fido2_credentials
    ]
    _options, _state = server.authenticate_begin(
        [_credential_data(credential) for credential in credentials],
        challenge=challenge,
    )
    allow_credentials = [
        _b64encode(credential.id)
        for credential in credentials
    ]
    user = _session_user(environ)
    registration_options = None
    user_credentials = []
    register_state = None
    if user is not None:
        register_options, register_state = server.register_begin(
            PublicKeyCredentialUserEntity(
                id=user.id.encode("utf-8"),
                name=user.id,
                display_name=user.id,
            ),
            [_credential_data(credential) for credential in user.fido2_credentials],
            user_verification="discouraged",
        )
        registration_options = _json_options(dict(register_options))
        user_credentials = [
            {
                "id": _credential_display_id(credential),
                "dt": credential.dt,
            }
            for credential in user.fido2_credentials
        ]
    google_csrf = secrets.token_urlsafe(32)
    status, headers, body = render(
        "auth.html",
        challenge=_b64encode(challenge),
        rp_id=_rp_id(environ),
        allow_credentials=allow_credentials,
        registration_options=registration_options,
        user_credentials=user_credentials,
        authenticated=user is not None,
        google_credentials=[
            {"email": credential.email}
            for credential in getattr(user, "google_credentials", [])
        ],
        google_csrf=google_csrf,
    )
    payload = {"state": _state, "issued": time()}
    if register_state is not None and user is not None:
        payload["register_state"] = register_state
        payload["register_user"] = user.id
    headers.append(_cookie(AUTHN_CHALLENGE_COOKIE, _signed(payload), max_age=_CHALLENGE_TTL_SECONDS, path="/authn"))
    headers.append(_cookie("google_csrf", google_csrf, max_age=_COOKIE_MAX_AGE_SECONDS, path="/authn"))
    return status, headers, body


def complete_authn(environ: dict):
    state = _authn_state_from_cookie(environ)
    if state is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication challenge is missing or expired.")
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
        credential = _find_credential(form.get("rawId", ""))
        if credential is None:
            raise ValueError("Unknown credential.")
        server = _server(environ)
        stored = _credential_data(credential)
        server.authenticate_complete(
            state,
            [stored],
            response=response,
        )
        serial = _b64encode(credential.id)
    except (binascii.Error, ValueError):
        return error(HTTPStatus.FORBIDDEN, "FIDO2 authentication failed.", traceback.format_exc())
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authenticated.")
    headers = [
        ("Location", "/"),
        _cookie(AUTHN_SESSION_COOKIE, _signed({"serial": serial, "issued": time()})),
        _clear_cookie(AUTHN_CHALLENGE_COOKIE, path="/authn"),
    ]
    return status, headers, body


def _server(environ: dict) -> Fido2Server:
    rp = PublicKeyCredentialRpEntity("dropzone-ticketing", _rp_id(environ))
    return Fido2Server(rp, verify_origin=lambda origin: origin == _origin(environ))


def _credential_data(credential: Fido2Credential):
    from fido2.webauthn import AttestedCredentialData

    return AttestedCredentialData(credential.data)


def complete_authn_register(environ: dict):
    if authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Authentication is disabled in registration-only mode.")
    user = _session_user(environ)
    if user is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication required.")
    register_state = _register_state_from_cookie(environ)
    if register_state is None:
        return error(HTTPStatus.FORBIDDEN, "Registration challenge is missing or expired.")
    state, state_user_id = register_state
    if not hmac.compare_digest(user.id, state_user_id):
        return error(HTTPStatus.FORBIDDEN, "Registration user does not match the challenge.")
    form = read_form(environ)
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
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_CHALLENGE_COOKIE, path="/authn")]
    return status, headers, body


def logout():
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Signed out.")
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_SESSION_COOKIE)]
    return status, headers, body


def require_auth(environ: dict):
    if authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Application is running in registration-only mode.")
    if _is_authenticated(environ):
        return None
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authentication required.")
    headers = [("Location", "/authn")]
    return status, headers, body

import fido2.features
fido2.features.webauthn_json_mapping.enabled = True
