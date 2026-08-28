from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http import HTTPStatus
from time import time
from urllib.parse import quote
from uuid import UUID

from bson import ObjectId
from bson.errors import InvalidId
from fido2.server import Fido2Server
from fido2.webauthn import (
    AuthenticatorAttestationResponse,
    AttestationObject,
    AttestationConveyancePreference,
    CollectedClientData,
    PublicKeyCredentialUserEntity,
    PublicKeyCredentialRpEntity,
    RegistrationResponse,
)

from .config import authn_config, session_secret
from .http import error, read_form, render
from ..model.auth import Fido2Credential, User
from ..model.auth import EmailAuthentication
from .email import code as generate_email_code, send_code

AUTHN_CHALLENGE_COOKIE = "authn_challenge"
AUTHN_SESSION_COOKIE = "authn_session"
AUTHN_CSRF_COOKIE = "authn_csrf"
_CHALLENGE_TTL_SECONDS = 300
_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60
_EMAIL_CODE_TTL_SECONDS = 300
_EMAIL_RESEND_DELAY_SECONDS = 60
EMAIL_PENDING_COOKIE = "email_authentication"


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


def _user_by_id_str(value: object) -> User | None:
    if not isinstance(value, str):
        return None
    try:
        user_id = ObjectId(value)
    except (InvalidId, TypeError):
        return None
    return User.objects(id=user_id).first()


def _is_authenticated(environ: dict) -> bool:
    payload = _unsign(_cookies(environ).get(AUTHN_SESSION_COOKIE, ""))
    if not payload:
        return False
    serial = payload.get("serial")
    issued = float(payload.get("issued", 0))
    return (
        (isinstance(serial, str) and _credential_owner(serial) is not None)
        or (_user_by_id_str(payload.get("user_id")) is not None)
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
        return _user_by_id_str(user_id)
    if not isinstance(serial, str):
        return None
    return _credential_owner(serial)


def current_user_id(environ: dict) -> str | None:
    session_user = _session_user(environ)
    user = session_user
    if user is None:
        return None
    return str(user.id)


def current_user_display_name(environ: dict) -> str | None:
    user = _session_user(environ)
    if user is None:
        return None
    return user.display_name or str(user.id)


def current_user_ref(environ: dict) -> dict[str, object] | None:
    user = _session_user(environ)
    if user is None:
        return None
    return {"id": user.id, "display_name": user.display_name or str(user.id)}


def _credential_display_id(credential: Fido2Credential) -> str:
    hex_value = credential.id.hex()
    if len(hex_value) <= 16:
        return hex_value
    return f"{hex_value[:8]}…{hex_value[-8:]}"


def _json_value(value: object):
    if isinstance(value, bytes):
        return _b64encode(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _aaguid_display(credential: Fido2Credential) -> str | None:
    aaguid = getattr(credential, "attestation_aaguid", None)
    if not aaguid:
        return None
    raw = bytes(aaguid)
    if len(raw) != 16:
        return raw.hex()
    return str(UUID(bytes=raw))


def _extensions_display(credential: Fido2Credential) -> str | None:
    extensions = getattr(credential, "extensions", None)
    if not extensions:
        return None
    try:
        return json.dumps(_json_value(dict(extensions)), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(extensions)


def _registration_fields(auth_data: object) -> dict[str, object]:
    """Extra credential fields taken from the authenticator data of a registration."""
    credential_data = getattr(auth_data, "credential_data", None)
    fields: dict[str, object] = {}
    aaguid = getattr(credential_data, "aaguid", None)
    if aaguid:
        fields["attestation_aaguid"] = bytes(aaguid)
    extensions = getattr(auth_data, "extensions", None)
    if extensions:
        fields["extensions"] = dict(extensions)
    return fields


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
                id=str(user.id).encode("utf-8"),
                name=str(user.id),
                display_name=user.display_name or str(user.id),
            ),
            [_credential_data(credential) for credential in user.fido2_credentials],
            user_verification="discouraged",
        )
        registration_options = _json_options(dict(register_options))
        user_credentials = [
            {
                "id": _credential_display_id(credential),
                "dt": credential.dt,
                "aaguid": _aaguid_display(credential),
                "extensions": _extensions_display(credential),
                "encoded_id": _b64encode(credential.id),
            }
            for credential in user.fido2_credentials
        ]
    email_pending = _email_pending(environ)
    authn_csrf = secrets.token_urlsafe(32)
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
        authn_csrf=authn_csrf,
        microsoft_credentials=[
            {"email": credential.email}
            for credential in getattr(user, "microsoft_credentials", [])
        ],
        current_display_name=user.display_name if user is not None else "",
        email=getattr(user, "email", "") if user is not None else "",
        email_pending=email_pending is not None,
        email_pending_address=email_pending.get("email") if email_pending else "",
        email_pending_purpose=email_pending.get("purpose") if email_pending else "",
    )
    payload = {"state": _state, "issued": time()}
    if register_state is not None and user is not None:
        payload["register_state"] = register_state
        payload["register_user"] = str(user.id)
    headers.append(_cookie(AUTHN_CHALLENGE_COOKIE, _signed(payload), max_age=_CHALLENGE_TTL_SECONDS, path="/authn"))
    headers.append(_cookie(AUTHN_CSRF_COOKIE, authn_csrf, max_age=_COOKIE_MAX_AGE_SECONDS, path="/authn"))
    return status, headers, body


def _email_pending(environ: dict) -> dict[str, object] | None:
    payload = _unsign(_cookies(environ).get(EMAIL_PENDING_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _EMAIL_CODE_TTL_SECONDS:
        return None
    if not isinstance(payload.get("user_id"), str) or not isinstance(payload.get("email"), str):
        return None
    return payload


def send_email_code(environ: dict):
    form = read_form(environ)
    requested = form.get("email", "").strip().casefold()
    if not requested or "@" not in requested or len(requested) > 320:
        return error(HTTPStatus.BAD_REQUEST, "A valid email address is required.")
    user = _session_user(environ)
    if user is None:
        user = User.objects(email=requested).first()
        if user is None:
            return error(HTTPStatus.FORBIDDEN, "This email address is not registered.")
        purpose_user_id = str(user.id)
    else:
        if user.email and requested == user.email.casefold():
            return error(HTTPStatus.BAD_REQUEST, "This is already your current email address.")
        existing = User.objects(email=requested).first()
        if existing is not None and existing.id != user.id:
            return error(HTTPStatus.CONFLICT, "This email address is already registered.")
        purpose_user_id = str(user.id)
    pending = getattr(user, "email_authentication", None)
    if pending and pending.email == requested and (datetime.now(timezone.utc) - pending.issued).total_seconds() < _EMAIL_RESEND_DELAY_SECONDS:
        return error(HTTPStatus.TOO_MANY_REQUESTS, "Please wait before requesting another code.")
    purpose = "change" if session_user is not None else "signin"
    new_code = generate_email_code()
    try:
        send_code(requested, new_code)
    except (OSError, smtplib.SMTPException, ValueError):
        return error(HTTPStatus.SERVICE_UNAVAILABLE, "Could not send the authentication email.")
    user.email_authentication = EmailAuthentication(
        email=requested,
        code=sha256(new_code.encode("ascii")).hexdigest(),
        purpose=purpose,
    )
    user.save()
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authentication code sent.")
    headers = [("Location", "/authn"), _cookie(
        EMAIL_PENDING_COOKIE,
        _signed({
            "user_id": purpose_user_id,
            "email": requested,
            "purpose": purpose,
            "issued": time(),
        }),
        max_age=_EMAIL_CODE_TTL_SECONDS,
        path="/authn",
    )]
    return status, headers, body


def verify_email_code(environ: dict):
    pending = _email_pending(environ)
    if pending is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication code is missing or expired.")
    user = _user_by_id_str(pending["user_id"])
    if user is None or getattr(user, "email_authentication", None) is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication code is missing or expired.")
    form = read_form(environ)
    supplied = form.get("code", "").strip()
    stored = user.email_authentication
    if len(supplied) != 6 or not supplied.isdigit():
        return error(HTTPStatus.FORBIDDEN, "Authentication code is invalid.")
    if (
        stored.email != pending["email"]
        or stored.purpose != pending.get("purpose")
        or time() - stored.issued.timestamp() > _EMAIL_CODE_TTL_SECONDS
    ):
        user.email_authentication = None
        user.save()
        return error(HTTPStatus.FORBIDDEN, "Authentication code is missing or expired.")
    if not secrets.compare_digest(stored.code, sha256(supplied.encode("ascii")).hexdigest()):
        return error(HTTPStatus.FORBIDDEN, "Authentication code is invalid.")
    new_email = stored.email
    if user.email != new_email:
        user.email = new_email
    user.email_authentication = None
    user.save()
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authenticated.")
    headers = [
        ("Location", "/authn"),
        _cookie(AUTHN_SESSION_COOKIE, _signed({"user_id": str(user.id), "issued": time()})),
        _clear_cookie(EMAIL_PENDING_COOKIE, path="/authn"),
    ]
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
    return Fido2Server(
        rp,
        attestation=AttestationConveyancePreference.ENTERPRISE,
        verify_origin=lambda origin: origin == _origin(environ),
    )


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
    if not hmac.compare_digest(str(user.id), state_user_id):
        return error(HTTPStatus.FORBIDDEN, "Registration user does not match the challenge.")
    form = read_form(environ)
    try:
        server = _server(environ)
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
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_CHALLENGE_COOKIE, path="/authn")]
    return status, headers, body


def logout():
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Signed out.")
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_SESSION_COOKIE)]
    return status, headers, body


def update_display_name(environ: dict):
    user = _session_user(environ)
    if user is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication required.")
    form = read_form(environ)
    display_name = form.get("display_name", "").strip()
    if len(display_name) > 200:
        return error(HTTPStatus.BAD_REQUEST, "Display name is too long.")
    user.display_name = display_name or None
    user.save()
    return HTTPStatus.SEE_OTHER, [("Location", "/authn")], b""


def remove_fido2_credential(environ: dict):
    user = _session_user(environ)
    if user is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication required.")
    form = read_form(environ)
    token = form.get("csrf", "")
    expected = _cookies(environ).get(AUTHN_CSRF_COOKIE, "")
    if not token or not expected or not secrets.compare_digest(token, expected):
        return error(HTTPStatus.FORBIDDEN, "Invalid request.")
    try:
        credential_id = _b64decode(form.get("credential_id", ""))
    except (ValueError, binascii.Error):
        return error(HTTPStatus.NOT_FOUND, "FIDO2 credential not found.")
    credentials = user.fido2_credentials
    user.fido2_credentials = [credential for credential in credentials if credential.id != credential_id]
    if len(user.fido2_credentials) == len(credentials):
        return error(HTTPStatus.NOT_FOUND, "FIDO2 credential not found.")
    user.save()
    return HTTPStatus.SEE_OTHER, [("Location", "/authn")], b""


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
