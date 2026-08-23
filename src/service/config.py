from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass

import mongoengine

from dropzone_ticketing.model import mongoengine_alias

MAX_TICKETS = 1000
MAX_CODE_ATTEMPTS = 20
MAX_FORM_BYTES = 1024 * 1024
CODE_LENGTH = 16
CODE_ALPHABET = "".join(chr(code_point) for code_point in range(33, 127))

_storage_connected = False
_storage_lock = threading.Lock()
_session_secret = secrets.token_bytes(32)


@dataclass(frozen=True)
class AuthnConfig:
    allowed_yubikey_ids: frozenset[str]


def allowed_yubikey_ids() -> frozenset[str]:
    value = os.environ.get("AUTHN_YUBIKEY_IDS", "")
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def authn_config() -> AuthnConfig:
    return AuthnConfig(allowed_yubikey_ids=allowed_yubikey_ids())


def session_secret() -> bytes:
    configured = os.environ.get("AUTHN_SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")
    return _session_secret


def mongodb_connection_string() -> str:
    return os.environ["MONGODB_CONNECTION_STRING"]


def ensure_storage() -> None:
    global _storage_connected
    if _storage_connected:
        return
    with _storage_lock:
        if not _storage_connected:
            mongoengine.register_connection(mongoengine_alias, host=mongodb_connection_string())
            _storage_connected = True
