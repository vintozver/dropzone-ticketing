from __future__ import annotations

import secrets
import threading
from http import HTTPStatus
from typing import Callable

import mongoengine
from bson import ObjectId

from dropzone_ticketing import PDF, Ticket
from dropzone_ticketing.model.auth import User
from dropzone_ticketing.model import mongoengine_alias
from dropzone_ticketing.model.ticket import UserRef

from . import auth as _auth_module
from .actions.issue import issue as _issue_action
from .actions.print_tickets import print_tickets as _print_tickets_action
from .actions.print_tickets import print_url as _print_url
from .actions.print_tickets import safe_filename as _safe_filename
from .actions.redeem import redeem as _redeem_action
from .actions.view_issued_tickets import view_issued_tickets as _view_issued_tickets_action
from .actions.view_owner_tickets import view_owner_tickets as _view_owner_tickets_action
from .actions.view_owners import view_owners as _view_owners_action
from .actions.view_redeemed_tickets import view_redeemed_tickets as _view_redeemed_tickets_action
from .actions.view_ticket import view_ticket as _view_ticket_action
from .actions.search_users import search_users as _search_users_action
from .config import CODE_ALPHABET, CODE_LENGTH, local_timezone, mongodb_uri
from .http import (
    exception_response,
    read_form as _read_form,
    render as _render,
    request_context,
    response_with_length,
)
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
            mongoengine.register_connection(mongoengine_alias, host=mongodb_uri())
            _storage_connected = True


def _issue(form: dict[str, str], issued_by: dict[str, object] | None = None):
    return _issue_action(
        form,
        ticket_class=Ticket,
        user_class=User,
        user_ref_class=UserRef,
        generate_code=generate_code,
        render=_render,
        print_url=_print_url,
        issued_by=issued_by,
    )


def _redeem(form: dict[str, str], by: dict[str, object] | None = None):
    return _redeem_action(form, ticket_class=Ticket, render=_render, split_codes=split_codes, user_ref_class=UserRef, by=by)


def _print_tickets(ticket_ids, user_id: str | None = None, display_name: str | None = None):
    return _print_tickets_action(
        ticket_ids,
        user_id,
        display_name,
        ticket_class=Ticket,
        pdf_class=PDF,
        render=_render,
        local_timezone=local_timezone(),
    )


def _view_owners():
    return _view_owners_action(ticket_class=Ticket, render=_render)


def _view_owner_tickets(user_id: str | None, display_name: str | None):
    return _view_owner_tickets_action(user_id, display_name, ticket_class=Ticket, render=_render)


def _view_ticket(ticket_id: str):
    return _view_ticket_action(ticket_id, ticket_class=Ticket, render=_render)


def _view_redeemed_tickets():
    return _view_redeemed_tickets_action(ticket_class=Ticket, render=_render)


def _view_issued_tickets():
    return _view_issued_tickets_action(ticket_class=Ticket, render=_render)


def _is_authenticated(environ: dict) -> bool:
    return _auth_module._is_authenticated(environ)


def _require_auth(environ: dict):
    return _auth_module.require_auth(environ)


def _current_user_id(environ: dict) -> str | None:
    return _auth_module.current_user_id(environ)


def _current_user_display_name(environ: dict) -> str | None:
    return _auth_module.current_user_display_name(environ)


def _current_user_ref(environ: dict) -> dict[str, object] | None:
    return _auth_module.current_user_ref(environ)


def _search_users(query: str):
    return _search_users_action(query, user_class=User)


def _method_not_allowed(allowed):
    from .http import method_not_allowed

    return method_not_allowed(allowed)


def _dispatch(environ: dict):
    return _route_dispatch(environ, handlers=__import__(__name__, fromlist=["dummy"]))


def application(environ: dict, start_response: Callable):
    """Serve the ticket issuing, redemption, and printing workflows."""
    with request_context(
        authenticated=_auth_module._is_authenticated(environ),
        current_user_id=_auth_module.current_user_id(environ),
        current_user_display_name=_auth_module.current_user_display_name(environ),
        registration_mode=_auth_module.authn_config().register,
    ):
        try:
            _ensure_storage()
            response = _dispatch(environ)
        except ValueError as error:
            response = _render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
        except Exception as exc:
            response = exception_response(exc)
    return response_with_length(response, start_response)
