from __future__ import annotations

import secrets
import os
import threading
from http import HTTPStatus
from typing import Callable

import mongoengine
from bson import ObjectId

from dropzone_ticketing import PDF, Ticket
from dropzone_ticketing.model import mongoengine_alias

from . import auth as _auth_module
from .actions.issue import issue as _issue_action
from .actions.print_tickets import print_tickets as _print_tickets_action
from .actions.print_tickets import print_url as _print_url
from .actions.print_tickets import safe_filename as _safe_filename
from .actions.redeem import redeem as _redeem_action
from .actions.view_owner_tickets import view_owner_tickets as _view_owner_tickets_action
from .actions.view_owners import view_owners as _view_owners_action
from .config import CODE_ALPHABET, CODE_LENGTH
from .http import exception_response, read_form as _read_form, render as _render, response_with_length
from .routes import dispatch as _route_dispatch

_storage_connected = False
_storage_lock = threading.Lock()


def generate_code() -> str:
    """Return a cryptographically secure printable-ASCII ticket code."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def split_codes(value: str) -> list[str]:
    """Split ticket codes on arbitrary whitespace."""
    return value.split()


def _ensure_storage() -> None:
    global _storage_connected
    if _storage_connected:
        return
    with _storage_lock:
        if not _storage_connected:
            mongoengine.register_connection(mongoengine_alias, host=os.environ["MONGODB_CONNECTION_STRING"])
            _storage_connected = True


def _issue(form: dict[str, str]):
    return _issue_action(form, ticket_class=Ticket, generate_code=generate_code, render=_render, print_url=_print_url)


def _redeem(form: dict[str, str]):
    return _redeem_action(form, ticket_class=Ticket, render=_render, split_codes=split_codes)


def _print_tickets(ticket_ids, owner: str | None = None):
    return _print_tickets_action(ticket_ids, owner, ticket_class=Ticket, pdf_class=PDF, render=_render)


def _view_owners():
    return _view_owners_action(ticket_class=Ticket, render=_render)


def _view_owner_tickets(owner: str):
    return _view_owner_tickets_action(owner, ticket_class=Ticket, render=_render)


def _is_authenticated(environ: dict) -> bool:
    return _auth_module._is_authenticated(environ)


def _require_auth(environ: dict):
    if _is_authenticated(environ):
        return None
    status, _headers, body = _render("error.html", HTTPStatus.SEE_OTHER, message="Authentication required.")
    return status, [("Location", "/authn")], body


def _verify_yubikey_attestation(*args, **kwargs):
    return _auth_module._verify_yubikey_attestation(*args, **kwargs)


def _method_not_allowed(allowed):
    from .http import method_not_allowed

    return method_not_allowed(allowed)


def _dispatch(environ: dict):
    return _route_dispatch(environ, handlers=__import__(__name__, fromlist=["dummy"]))


def application(environ: dict, start_response: Callable):
    """Serve the ticket issuing, redemption, and printing workflows."""
    try:
        _ensure_storage()
        response = _dispatch(environ)
    except ValueError as error:
        response = _render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
    except Exception as exc:
        response = exception_response(exc)
    return response_with_length(response, start_response)
