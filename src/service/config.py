from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

MAX_TICKETS = 1000
MAX_CODE_ATTEMPTS = 20
MAX_FORM_BYTES = 1024 * 1024
CODE_LENGTH = 16
CODE_ALPHABET = "".join(chr(code_point) for code_point in range(33, 127))

_session_secret = secrets.token_bytes(32)


@dataclass(frozen=True)
class AuthnConfig:
    register: bool


def registration_mode() -> bool:
    return "REGISTRATION_MODE" in os.environ


def authn_config() -> AuthnConfig:
    return AuthnConfig(register=registration_mode())


def session_secret() -> bytes:
    configured = os.environ.get("AUTHN_SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")
    return _session_secret


def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def google_client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def google_redirect_uri(environ: dict) -> str:
    configured = os.environ.get("GOOGLE_REDIRECT_URI")
    if configured:
        return configured
    scheme = environ.get("wsgi.url_scheme") or "http"
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "localhost"
    return f"{scheme}://{host}/authn/google/callback"


def mongodb_uri() -> str:
    return os.environ["MONGODB_URI"]
