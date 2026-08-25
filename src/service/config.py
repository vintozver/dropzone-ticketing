from __future__ import annotations

import os
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

MAX_TICKETS = 1000
MAX_CODE_ATTEMPTS = 20
MAX_FORM_BYTES = 1024 * 1024
CODE_LENGTH = 16
CODE_ALPHABET = "".join(chr(code_point) for code_point in range(33, 127))

_session_secret = secrets.token_bytes(32)


def _file_config() -> dict[str, object]:
    filename = os.environ.get("CONFIG_FILE", "config.json")
    path = Path(filename)
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as config_file:
        values = json.load(config_file)
    if not isinstance(values, dict):
        raise ValueError("Configuration file must contain a JSON object.")
    return values


def _setting(name: str, default=None):
    if name in os.environ:
        return os.environ[name]
    return _file_config().get(name, default)


@dataclass(frozen=True)
class AuthnConfig:
    register: bool


def registration_mode() -> bool:
    if "REGISTRATION_MODE" in os.environ:
        return True
    return bool(_setting("REGISTRATION_MODE", False))


def authn_config() -> AuthnConfig:
    return AuthnConfig(register=registration_mode())


def session_secret() -> bytes:
    configured = _setting("AUTHN_SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")
    return _session_secret


def google_client_id() -> str:
    return str(_setting("GOOGLE_CLIENT_ID", ""))


def google_client_secret() -> str:
    return str(_setting("GOOGLE_CLIENT_SECRET", ""))


def google_redirect_uri(environ: dict) -> str:
    configured = _setting("GOOGLE_REDIRECT_URI")
    if configured:
        return configured
    scheme = environ.get("wsgi.url_scheme") or "http"
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "localhost"
    return f"{scheme}://{host}/authn/google/callback"


def mongodb_uri() -> str:
    configured = _setting("MONGODB_URI")
    if not configured:
        raise KeyError("MONGODB_URI")
    return str(configured)
