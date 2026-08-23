from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import traceback
from hashlib import sha256
from http import HTTPStatus
from time import time
from urllib.parse import quote

from fido2.server import Fido2Server
from fido2.webauthn import (
    PublicKeyCredentialRpEntity,
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


def _cookie(name: str, value: str, *, max_age: int = _COOKIE_MAX_AGE_SECONDS, path: str = "/") -> tuple[str, str]:
    return (
        "Set-Cookie",
        f"{name}={quote(value, safe='')}; Max-Age={max_age}; Path={path}; Secure; HttpOnly; SameSite=Strict",
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


def _challenge_from_cookie(environ: dict) -> bytes | None:
    payload = _unsign(_cookies(environ).get(AUTHN_CHALLENGE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _CHALLENGE_TTL_SECONDS:
        return None
    challenge = payload.get("challenge")
    if not isinstance(challenge, str):
        return None
    try:
        return _b64decode(challenge)
    except ValueError:
        return None


def _is_authenticated(environ: dict) -> bool:
    if authn_config().register:
        return True
    payload = _unsign(_cookies(environ).get(AUTHN_SESSION_COOKIE, ""))
    if not payload:
        return False
    serial = payload.get("serial")
    issued = float(payload.get("issued", 0))
    return (
        isinstance(serial, str)
        and _find_credential(serial) is not None
        and time() - issued <= _COOKIE_MAX_AGE_SECONDS
    )


def _find_credential(encoded_id: str) -> Fido2Credential | None:
    try:
        credential_id = _b64decode(encoded_id)
    except (ValueError, binascii.Error):
        return None
    user = User.objects(fido2_credentials__credential_id=credential_id).first()
    if user is None:
        return None
    return next(
        (credential for credential in user.fido2_credentials if credential.credential_id == credential_id),
        None,
    )


def begin_authn(environ: dict):
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
        _b64encode(credential.credential_id)
        for credential in credentials
    ]
    status, headers, body = render(
        "auth.html",
        challenge=_b64encode(challenge),
        rp_id=_rp_id(environ),
        allow_credentials=allow_credentials,
    )
    payload = {"challenge": _b64encode(challenge), "issued": time()}
    headers.append(_cookie(AUTHN_CHALLENGE_COOKIE, _signed(payload), max_age=_CHALLENGE_TTL_SECONDS, path="/authn"))
    return status, headers, body


def complete_authn(environ: dict):
    challenge = _challenge_from_cookie(environ)
    if challenge is None:
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
        credential_id = _b64decode(form.get("rawId", ""))
        credential = _find_credential(form.get("rawId", ""))
        if credential is None:
            raise ValueError("Unknown credential.")
        server = _server(environ)
        stored = _credential_data(credential)
        server.authenticate_complete(
            {"challenge": _b64encode(challenge), "user_verification": None},
            [stored],
            response=response,
        )
        serial = _b64encode(credential_id)
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

    return AttestedCredentialData(credential.credential_data)


def logout():
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Signed out.")
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_SESSION_COOKIE)]
    return status, headers, body


def require_auth(environ: dict):
    if authn_config().register:
        return None
    if _is_authenticated(environ):
        return None
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authentication required.")
    headers = [("Location", "/authn")]
    return status, headers, body

import fido2.features
fido2.features.webauthn_json_mapping.enabled = True
