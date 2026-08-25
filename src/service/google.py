from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from time import time
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen

from .config import google_client_id, google_client_secret, google_redirect_uri
from .http import error, read_form
from .auth import (
    AUTHN_SESSION_COOKIE,
    _COOKIE_MAX_AGE_SECONDS,
    _cookie,
    _cookies,
    _session_user,
    _signed,
    _unsign,
)
from dropzone_ticketing.model.auth import GoogleCredential, User

GOOGLE_STATE_COOKIE = "google_oauth_state"
_GOOGLE_STATE_TTL_SECONDS = 300


def _configured() -> bool:
    return bool(google_client_id() and google_client_secret())


def begin(environ: dict):
    if not _configured():
        return error(HTTPStatus.NOT_IMPLEMENTED, "Google authentication is not configured.")
    state = secrets.token_urlsafe(32)
    user = _session_user(environ)
    payload = {"state": state, "issued": time(), "user": user.id if user else None}
    query = urlencode(
        {
            "client_id": google_client_id(),
            "redirect_uri": google_redirect_uri(environ),
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return (
        HTTPStatus.SEE_OTHER,
        [
            ("Location", f"https://accounts.google.com/o/oauth2/v2/auth?{query}"),
            _cookie(GOOGLE_STATE_COOKIE, _signed(payload), max_age=_GOOGLE_STATE_TTL_SECONDS, path="/authn/google"),
        ],
        b"",
    )


def _state(environ: dict) -> dict[str, object] | None:
    payload = _unsign(_cookies(environ).get(GOOGLE_STATE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _GOOGLE_STATE_TTL_SECONDS:
        return None
    return payload


def _post_token(code: str, redirect_uri: str) -> dict:
    body = urlencode(
        {
            "code": code,
            "client_id": google_client_id(),
            "client_secret": google_client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    with urlopen(Request("https://oauth2.googleapis.com/token", data=body, method="POST"), timeout=10) as response:
        return json.loads(response.read())


def _google_email(access_token: str) -> str:
    request = Request("https://openidconnect.googleapis.com/v1/userinfo")
    request.add_header("Authorization", "Bearer " + access_token)
    with urlopen(request, timeout=10) as response:
        profile = json.loads(response.read())
    email = profile.get("email")
    if not isinstance(email, str) or not email or not profile.get("email_verified", False):
        raise ValueError("Google did not provide a verified email address.")
    return email.casefold()


def complete(environ: dict):
    state = _state(environ)
    query = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
    if state is None or not secrets.compare_digest(str(state.get("state", "")), query.get("state", "")):
        return error(HTTPStatus.FORBIDDEN, "Google authentication state is missing or invalid.")
    if query.get("error") or not query.get("code"):
        return error(HTTPStatus.FORBIDDEN, "Google authentication was cancelled or failed.")
    try:
        token = _post_token(query["code"], google_redirect_uri(environ))
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Google token response did not contain an access token.")
        email = _google_email(access_token)
    except (KeyError, ValueError, OSError, json.JSONDecodeError):
        return error(HTTPStatus.FORBIDDEN, "Google authentication failed.")

    user = _session_user(environ)
    if user is not None and state.get("user") == user.id:
        if any(credential.email.casefold() == email for credential in user.google_credentials):
            return error(HTTPStatus.CONFLICT, "Google credential is already registered.")
        user.google_credentials.append(GoogleCredential(email=email))
        user.save()
    else:
        user = User.objects(google_credentials__email=email).first()
        if user is None:
            return error(HTTPStatus.FORBIDDEN, "This Google account is not registered.")
    return (
        HTTPStatus.SEE_OTHER,
        [
            ("Location", "/authn"),
            _cookie(AUTHN_SESSION_COOKIE, _signed({"user_id": user.id, "issued": time()}), max_age=_COOKIE_MAX_AGE_SECONDS),
            _cookie(GOOGLE_STATE_COOKIE, "", max_age=0, path="/authn/google"),
        ],
        b"",
    )


def remove(environ: dict):
    user = _session_user(environ)
    if user is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication required.")
    email = read_form(environ).get("email", "").strip().casefold()
    credentials = user.google_credentials
    user.google_credentials = [credential for credential in credentials if credential.email.casefold() != email]
    if len(user.google_credentials) == len(credentials):
        return error(HTTPStatus.NOT_FOUND, "Google credential not found.")
    user.save()
    return HTTPStatus.SEE_OTHER, [("Location", "/authn")], b""
