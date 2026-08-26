from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

MAX_TICKETS = 1000
MAX_CODE_ATTEMPTS = 20
MAX_FORM_BYTES = 1024 * 1024
CODE_LENGTH = 16
CODE_ALPHABET = "".join(chr(code_point) for code_point in range(33, 127))

_session_secret = secrets.token_bytes(32)


@lru_cache(maxsize=8)
def _file_config(filename: str) -> dict[str, object]:
    path = Path(filename)
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as config_file:
        values = yaml.safe_load(config_file)
    if not isinstance(values, dict):
        raise ValueError("Configuration file must contain a YAML object.")
    return values


def _config() -> dict[str, object]:
    return _file_config(os.environ.get("CONFIG_FILE", "config.yaml"))


def _section(name: str) -> dict[str, object]:
    values = _config().get(name)
    return values if isinstance(values, dict) else {}


def _setting(name: str, default=None):
    return _config().get(name, default)


@dataclass(frozen=True)
class AuthnConfig:
    register: bool


def registration_mode() -> bool:
    return bool(_setting("registration_mode", False))


def local_timezone() -> ZoneInfo:
    return ZoneInfo(str(_setting("timezone", "UTC") or "UTC"))


def business_name() -> str:
    return str(_setting("business_name", "The Dropzone") or "The Dropzone")


def authn_config() -> AuthnConfig:
    return AuthnConfig(register=registration_mode())


def session_secret() -> bytes:
    return _session_secret


def google_client_id() -> str:
    return str(_google_setting("client_id", ""))


def google_client_secret() -> str:
    return str(_google_setting("secret", ""))


def google_redirect_uri(environ: dict) -> str:
    configured = _google_setting("redirect_uri")
    if configured:
        return configured
    scheme = environ.get("wsgi.url_scheme") or "http"
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "localhost"
    return f"{scheme}://{host}/authn/google/callback"


def _google_setting(name: str, default=None):
    return _section("google").get(name, default)


def microsoft_client_id() -> str:
    return str(_microsoft_setting("client_id", ""))


def microsoft_client_secret() -> str:
    return str(_microsoft_setting("secret", ""))


def microsoft_client_certificate() -> str:
    return str(_microsoft_setting("certificate", "") or "")


def microsoft_redirect_uri(environ: dict) -> str:
    configured = _microsoft_setting("redirect_uri")
    if configured:
        return configured
    scheme = environ.get("wsgi.url_scheme") or "http"
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "localhost"
    return f"{scheme}://{host}/authn/microsoft/callback"


def _microsoft_setting(name: str, default=None):
    return _section("microsoft").get(name, default)


def mongodb_uri() -> str:
    configured = _setting("mongodb_uri")
    if not configured:
        raise KeyError("mongodb_uri")
    return str(configured)
