from __future__ import annotations

import os
import secrets
import string
import traceback
from functools import lru_cache
from http import HTTPStatus
from time import time
from urllib.parse import parse_qs

import requests
from google.auth.exceptions import GoogleAuthError
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import Error as GoogleApiError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

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
# The cookie must survive Google's cross-site redirect back to the callback.
_GOOGLE_STATE_SAME_SITE = "Lax"
_GOOGLE_DISCOVERY_URI = "https://accounts.google.com/.well-known/openid-configuration"
_GOOGLE_SCOPES = ["email"]
_CODE_VERIFIER_LENGTH = 128
_CODE_VERIFIER_ALPHABET = string.ascii_letters + string.digits

# Google grants the canonical "https://www.googleapis.com/auth/userinfo.email" scope for the
# requested "email" scope, which oauthlib rejects as a scope change unless relaxed.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def _configured() -> bool:
    return bool(google_client_id() and google_client_secret())


def _generate_code_verifier() -> str:
    return "".join(secrets.choice(_CODE_VERIFIER_ALPHABET) for _ in range(_CODE_VERIFIER_LENGTH))


@lru_cache(maxsize=1)
def _endpoints() -> tuple[str, str]:
    response = requests.get(_GOOGLE_DISCOVERY_URI, timeout=10)
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise ValueError("Google discovery document is invalid.")
    auth_uri = document["authorization_endpoint"]
    token_uri = document["token_endpoint"]
    if not isinstance(auth_uri, str) or not isinstance(token_uri, str):
        raise ValueError("Google discovery document is invalid.")
    return auth_uri, token_uri


def _oauth_flow(redirect_uri: str, code_verifier: str) -> Flow:
    auth_uri, token_uri = _endpoints()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": google_client_id(),
                "client_secret": google_client_secret(),
                "auth_uri": auth_uri,
                "token_uri": token_uri,
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=_GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.code_verifier = code_verifier
    return flow


def begin(environ: dict):
    if not _configured():
        return error(HTTPStatus.NOT_IMPLEMENTED, "Google authentication is not configured.")
    state = secrets.token_urlsafe(32)
    code_verifier = _generate_code_verifier()
    user = _session_user(environ)
    payload = {
        "state": state,
        "issued": time(),
        "user": str(user.id) if user else None,
        "code_verifier": code_verifier,
    }
    redirect_uri = google_redirect_uri(environ)
    try:
        query, _ = _oauth_flow(redirect_uri, code_verifier).authorization_url(state=state, prompt="select_account")
    except (GoogleAuthError, GoogleApiError, KeyError, ValueError, requests.RequestException):
        return error(HTTPStatus.FORBIDDEN, "Google authentication failed.", traceback.format_exc())
    return (
        HTTPStatus.SEE_OTHER,
        [
            ("Location", query),
            _cookie(
                GOOGLE_STATE_COOKIE,
                _signed(payload),
                max_age=_GOOGLE_STATE_TTL_SECONDS,
                path="/authn/google",
                same_site=_GOOGLE_STATE_SAME_SITE,
            ),
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
    if not isinstance(email, str) or not email or not profile.get("verified_email", False):
        raise ValueError("Google did not provide a verified email address.")
    return email.casefold()


def complete(environ: dict):
    state = _state(environ)
    query = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
    if state is None or not secrets.compare_digest(str(state.get("state", "")), query.get("state", "")):
        return error(HTTPStatus.FORBIDDEN, "Google authentication state is missing or invalid.")
    if query.get("error") or not query.get("code"):
        return error(HTTPStatus.FORBIDDEN, "Google authentication was cancelled or failed.")
    code_verifier = state.get("code_verifier")
    if not isinstance(code_verifier, str) or not code_verifier:
        return error(HTTPStatus.FORBIDDEN, "Google authentication state is missing or invalid.")
    try:
        redirect_uri = google_redirect_uri(environ)
        flow = _oauth_flow(redirect_uri, code_verifier)
        flow.fetch_token(code=query["code"])
        oauth2_service = build("oauth2", "v2", credentials=flow.credentials)
        profile = oauth2_service.userinfo().get().execute()
        if not isinstance(profile, dict):
            raise ValueError("Google profile response is invalid.")
        email = _google_email(profile)
    except (GoogleAuthError, GoogleApiError, OAuth2Error, KeyError, ValueError, requests.RequestException):
        return error(HTTPStatus.FORBIDDEN, "Google authentication failed.", traceback.format_exc())

    user = _session_user(environ)
    if user is not None and state.get("user") == str(user.id):
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
            _cookie(AUTHN_SESSION_COOKIE, _signed({"user_id": str(user.id), "issued": time()}), max_age=_COOKIE_MAX_AGE_SECONDS),
            _cookie(GOOGLE_STATE_COOKIE, "", max_age=0, path="/authn/google", same_site=_GOOGLE_STATE_SAME_SITE),
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
