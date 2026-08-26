from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import string
import traceback
import uuid
from functools import lru_cache
from http import HTTPStatus
from time import time
from urllib.parse import urlencode, parse_qs

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509 import load_pem_x509_certificate

from .auth import AUTHN_CSRF_COOKIE, AUTHN_SESSION_COOKIE, _COOKIE_MAX_AGE_SECONDS, _cookie, _cookies, _session_user, _signed, _unsign
from .config import microsoft_client_id, microsoft_client_key, microsoft_client_secret, microsoft_redirect_uri
from .http import error, read_form
from ..model.auth import MicrosoftCredential, User

MICROSOFT_STATE_COOKIE = "microsoft_oauth_state"
_STATE_TTL_SECONDS = 300
_DISCOVERY_URI = "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
_SCOPES = "openid email"
_CODE_VERIFIER_LENGTH = 128
_ALPHABET = string.ascii_letters + string.digits
_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_ASSERTION_TTL_SECONDS = 300
_CERTIFICATE_PEM = re.compile(
    r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL
)


def _configured() -> bool:
    return bool(microsoft_client_id() and (microsoft_client_key() or microsoft_client_secret()))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _certificate_thumbprint(pem: str) -> str | None:
    match = _CERTIFICATE_PEM.search(pem)
    if match is None:
        return None
    certificate = load_pem_x509_certificate(match.group(0).encode())
    # RFC 7515 defines "x5t" as the SHA-1 thumbprint; Microsoft requires that exact value.
    return _b64url(certificate.fingerprint(hashes.SHA1()))


def _client_assertion(token_endpoint: str) -> str:
    """Build a JWT signed with the configured private key, as Microsoft expects."""
    pem = microsoft_client_key()
    try:
        private_key = serialization.load_pem_private_key(pem.encode(), None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Microsoft requires an RSA private key.")
        header = {"alg": "RS256", "typ": "JWT"}
        thumbprint = _certificate_thumbprint(pem)
        if thumbprint:
            header["x5t"] = thumbprint
    except Exception:
        # Never surface the key material or its parsing details.
        raise ValueError("Microsoft private key is invalid.") from None
    client_id = microsoft_client_id()
    issued = int(time())
    claims = {
        "aud": token_endpoint, "iss": client_id, "sub": client_id, "jti": str(uuid.uuid4()),
        "iat": issued, "nbf": issued, "exp": issued + _ASSERTION_TTL_SECONDS,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    ).encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(signature)}"


def _client_authentication(token_endpoint: str) -> dict[str, str]:
    """Private key authentication takes precedence over the client secret."""
    if microsoft_client_key():
        return {
            "client_assertion_type": _CLIENT_ASSERTION_TYPE,
            "client_assertion": _client_assertion(token_endpoint),
        }
    return {"client_secret": microsoft_client_secret()}


def _generate_code_verifier() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_VERIFIER_LENGTH))


@lru_cache(maxsize=1)
def _endpoints() -> tuple[str, str, str]:
    response = requests.get(_DISCOVERY_URI, timeout=10)
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise ValueError("Microsoft discovery document is invalid.")
    try:
        values = tuple(document[key] for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"))
    except KeyError as exc:
        raise ValueError("Microsoft discovery document is invalid.") from exc
    if not all(isinstance(value, str) for value in values):
        raise ValueError("Microsoft discovery document is invalid.")
    return values


def begin(environ: dict):
    if not _configured():
        return error(HTTPStatus.NOT_IMPLEMENTED, "Microsoft authentication is not configured.")
    state = secrets.token_urlsafe(32)
    verifier = _generate_code_verifier()
    user = _session_user(environ)
    payload = {"state": state, "issued": time(), "user": str(user.id) if user else None, "code_verifier": verifier}
    redirect_uri = microsoft_redirect_uri(environ)
    try:
        authorization_endpoint, _token_endpoint, _userinfo_endpoint = _endpoints()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = urlencode({
            "client_id": microsoft_client_id(), "response_type": "code", "redirect_uri": redirect_uri,
            "response_mode": "query", "scope": _SCOPES, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
    except (KeyError, ValueError, requests.RequestException):
        return error(HTTPStatus.FORBIDDEN, "Microsoft authentication failed.", traceback.format_exc())
    return HTTPStatus.SEE_OTHER, [
        ("Location", f"{authorization_endpoint}?{query}"),
        _cookie(MICROSOFT_STATE_COOKIE, _signed(payload), max_age=_STATE_TTL_SECONDS, path="/authn/microsoft"),
    ], b""


def _state(environ: dict) -> dict[str, object] | None:
    payload = _unsign(_cookies(environ).get(MICROSOFT_STATE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _STATE_TTL_SECONDS:
        return None
    return payload


def _email(profile: dict[str, object]) -> str:
    value = profile.get("email") or profile.get("preferred_username")
    if not isinstance(value, str) or not value:
        raise ValueError("Microsoft did not provide an email address.")
    return value.casefold()


def complete(environ: dict):
    state = _state(environ)
    query = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
    if state is None or not secrets.compare_digest(str(state.get("state", "")), query.get("state", "")):
        return error(HTTPStatus.FORBIDDEN, "Microsoft authentication state is missing or invalid.")
    if query.get("error") or not query.get("code"):
        return error(HTTPStatus.FORBIDDEN, "Microsoft authentication was cancelled or failed.")
    verifier = state.get("code_verifier")
    if not isinstance(verifier, str) or not verifier:
        return error(HTTPStatus.FORBIDDEN, "Microsoft authentication state is missing or invalid.")
    try:
        redirect_uri = microsoft_redirect_uri(environ)
        _authorization_endpoint, token_endpoint, userinfo_endpoint = _endpoints()
        token = requests.post(token_endpoint, data={
            "client_id": microsoft_client_id(),
            "code": query["code"], "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            "scope": _SCOPES, "code_verifier": verifier, **_client_authentication(token_endpoint),
        }, timeout=10)
        token.raise_for_status()
        token_data = token.json()
        access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Microsoft token response is invalid.")
        profile_response = requests.get(
            userinfo_endpoint, headers={"Authorization": "Bearer " + access_token}, timeout=10
        )
        profile_response.raise_for_status()
        profile = profile_response.json()
        if not isinstance(profile, dict):
            raise ValueError("Microsoft profile response is invalid.")
        email = _email(profile)
    except (KeyError, ValueError, requests.RequestException):
        return error(HTTPStatus.FORBIDDEN, "Microsoft authentication failed.", traceback.format_exc())
    user = _session_user(environ)
    if user is not None and state.get("user") == str(user.id):
        if any(credential.email.casefold() == email for credential in user.microsoft_credentials):
            return error(HTTPStatus.CONFLICT, "Microsoft credential is already registered.")
        user.microsoft_credentials.append(MicrosoftCredential(email=email))
        user.save()
    else:
        user = User.objects(microsoft_credentials__email=email).first()
        if user is None:
            return error(HTTPStatus.FORBIDDEN, "This Microsoft account is not registered.")
    return HTTPStatus.SEE_OTHER, [
        ("Location", "/authn"),
        _cookie(AUTHN_SESSION_COOKIE, _signed({"user_id": str(user.id), "issued": time()}), max_age=_COOKIE_MAX_AGE_SECONDS),
        _cookie(MICROSOFT_STATE_COOKIE, "", max_age=0, path="/authn/microsoft"),
    ], b""


def remove(environ: dict):
    user = _session_user(environ)
    if user is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication required.")
    form = read_form(environ)
    if not secrets.compare_digest(form.get("csrf", ""), _cookies(environ).get(AUTHN_CSRF_COOKIE, "")):
        return error(HTTPStatus.FORBIDDEN, "Invalid request.")
    email = form.get("email", "").strip().casefold()
    credentials = user.microsoft_credentials
    user.microsoft_credentials = [credential for credential in credentials if credential.email.casefold() != email]
    if len(user.microsoft_credentials) == len(credentials):
        return error(HTTPStatus.NOT_FOUND, "Microsoft credential not found.")
    user.save()
    return HTTPStatus.SEE_OTHER, [("Location", "/authn")], b""
