from __future__ import annotations

import secrets
from http import HTTPStatus
from time import time
from urllib.parse import parse_qs

import httplib2
from oauth2client.client import FlowExchangeError, OAuth2WebServerFlow, verify_id_token
from oauth2client.crypt import AppIdentityError

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
GOOGLE_CSRF_COOKIE = "google_csrf"
_GOOGLE_STATE_TTL_SECONDS = 300
_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_HTTP = httplib2.Http()


def _configured() -> bool:
    return bool(google_client_id() and google_client_secret())


def _oauth_flow(redirect_uri: str) -> OAuth2WebServerFlow:
    return OAuth2WebServerFlow(
        client_id=google_client_id(),
        client_secret=google_client_secret(),
        scope="openid email",
        redirect_uri=redirect_uri,
        auth_uri=_GOOGLE_AUTH_URI,
        token_uri=_GOOGLE_TOKEN_URI,
        prompt="select_account",
    )


def begin(environ: dict):
    if not _configured():
        return error(HTTPStatus.NOT_IMPLEMENTED, "Google authentication is not configured.")
    state = secrets.token_urlsafe(32)
    user = _session_user(environ)
    payload = {"state": state, "issued": time(), "user": user.id if user else None}
    redirect_uri = google_redirect_uri(environ)
    query = _oauth_flow(redirect_uri).step1_get_authorize_url(state=state)
    return (
        HTTPStatus.SEE_OTHER,
        [
            ("Location", query),
            _cookie(GOOGLE_STATE_COOKIE, _signed(payload), max_age=_GOOGLE_STATE_TTL_SECONDS, path="/authn/google"),
        ],
        b"",
    )


def _state(environ: dict) -> dict[str, object] | None:
    payload = _unsign(_cookies(environ).get(GOOGLE_STATE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _GOOGLE_STATE_TTL_SECONDS:
        return None
    return payload


def _google_email(profile: dict[str, object]) -> str:
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
        redirect_uri = google_redirect_uri(environ)
        token = _oauth_flow(redirect_uri).step2_exchange(query["code"]).token_response
        if not isinstance(token, dict):
            raise ValueError("Google token response is invalid.")
        id_token = token.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ValueError("Google token response did not contain an ID token.")
        profile = verify_id_token(id_token, google_client_id(), http=_HTTP)
        if not isinstance(profile, dict):
            raise ValueError("Google ID token response is invalid.")
        email = _google_email(profile)
    except (AppIdentityError, FlowExchangeError, KeyError, ValueError, httplib2.HttpLib2Error):
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
    form = read_form(environ)
    token = form.get("csrf", "")
    expected = _cookies(environ).get(GOOGLE_CSRF_COOKIE, "")
    if not token or not expected or not secrets.compare_digest(token, expected):
        return error(HTTPStatus.FORBIDDEN, "Invalid request.")
    email = form.get("email", "").strip().casefold()
    credentials = user.google_credentials
    user.google_credentials = [credential for credential in credentials if credential.email.casefold() != email]
    if len(user.google_credentials) == len(credentials):
        return error(HTTPStatus.NOT_FOUND, "Google credential not found.")
    user.save()
    return HTTPStatus.SEE_OTHER, [("Location", "/authn")], b""
