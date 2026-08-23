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


def auth_register() -> bool:
    return "AUTH_REGISTER" in os.environ


def authn_config() -> AuthnConfig:
    return AuthnConfig(register=auth_register())


def session_secret() -> bytes:
    configured = os.environ.get("AUTHN_SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")
    return _session_secret


def mongodb_connection_string() -> str:
    return os.environ["MONGODB_CONNECTION_STRING"]
