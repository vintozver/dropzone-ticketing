from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Callable

import mongoengine
from bson import ObjectId

from .. import PDF, Ticket
from ..model import mongoengine_alias
from ..model.auth import User
from ..model.ticket import UserRef
from ..model.event import Event, EventType, migrate_events

from . import auth as _auth_module
from .actions.admin_users import admin_index as _admin_index_action
from .actions.admin_users import create_user as _create_user_action
from .actions.admin_users import list_users as _list_users_action
from .actions.admin_users import new_user as _new_user_action
from .actions.admin_users import update_user as _update_user_action
from .actions.admin_users import view_user as _view_user_action
from .actions.admin_events import create_event as _create_event_action
from .actions.admin_events import create_event_type as _create_event_type_action
from .actions.admin_events import list_events as _list_events_action
from .actions.admin_events import list_event_types as _list_event_types_action
from .actions.admin_events import new_event as _new_event_action
from .actions.admin_events import update_event as _update_event_action
from .actions.admin_events import update_event_type as _update_event_type_action
from .actions.admin_events import view_event as _view_event_action
from .actions.admin_events import view_event_type as _view_event_type_action
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
from .actions.partner import create as _create_partner
from .actions.partner import update as _update_partner
from .actions.partner import view_partner as _view_partner
from .actions.partner import view_partners as _view_partners
from .actions.booking import availability as _booking_availability_action
from .actions.booking import complete as _booking_complete_action
from .actions.booking import contact as _booking_contact_action
from .actions.booking import hold as _booking_hold_action
from .config import (
    CODE_ALPHABET,
    CODE_LENGTH,
    local_timezone,
    mongodb_uri,
    stripe_publishable_key,
)
from . import payment as _payment_provider
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
            mongoengine.register_connection(
                mongoengine_alias,
                host=mongodb_uri(),
                tz_aware=True,
            )
            migrate_events()
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
    )


def _view_owners():
    return _view_owners_action(ticket_class=Ticket, render=_render)


def _view_owner_tickets(user_id: str | None, display_name: str | None):
    return _view_owner_tickets_action(user_id, display_name, ticket_class=Ticket, render=_render)


def _view_ticket(ticket_id: str, viewer):
    return _view_ticket_action(ticket_id, viewer, ticket_class=Ticket, render=_render)


def _view_redeemed_tickets():
    return _view_redeemed_tickets_action(ticket_class=Ticket, render=_render)


def _view_issued_tickets():
    return _view_issued_tickets_action(ticket_class=Ticket, render=_render)


def _is_authenticated(environ: dict) -> bool:
    return _auth_module._is_authenticated(environ)


def _require_auth(environ: dict):
    return _auth_module.require_auth(environ)


def _require_admin(environ: dict):
    return _auth_module.require_role(environ, "admin")


def _current_user_id(environ: dict) -> str | None:
    return _auth_module.current_user_id(environ)


def _current_user_display_name(environ: dict) -> str | None:
    return _auth_module.current_user_display_name(environ)


def _current_user_ref(environ: dict) -> dict[str, object] | None:
    return _auth_module.current_user_ref(environ)


def _search_users(query: str):
    return _search_users_action(query, user_class=User)


def _create_user(form: dict[str, str]):
    return _create_user_action(
        form,
        user_class=User,
        google_credential_class=_auth_module.GoogleCredential,
        microsoft_credential_class=_auth_module.MicrosoftCredential,
        render=_render,
    )


def _new_user():
    return _new_user_action(render=_render)


def _admin_index():
    return _admin_index_action(render=_render)


def _list_users():
    return _list_users_action(user_class=User, render=_render)


def _view_user(user_id: str):
    return _view_user_action(user_id, user_class=User, render=_render)


def _update_user(user_id: str, form: dict[str, str]):
    return _update_user_action(user_id, form, user_class=User, render=_render)


def _list_events():
    return _list_events_action(event_class=Event, render=_render)


def _new_event():
    return _new_event_action(event_type_class=EventType, render=_render)


def _create_event(form: dict[str, str], user):
    return _create_event_action(
        form,
        user,
        event_class=Event,
        event_type_class=EventType,
        render=_render,
        local_timezone=local_timezone(),
        now=datetime.now(timezone.utc),
    )


def _view_event(event_id: str):
    return _view_event_action(
        event_id,
        event_class=Event,
        event_type_class=EventType,
        render=_render,
        local_timezone=local_timezone(),
    )


def _update_event(event_id: str, form: dict[str, str], user):
    return _update_event_action(
        event_id,
        form,
        user,
        event_class=Event,
        event_type_class=EventType,
        payment_provider=_payment_provider,
        render=_render,
        local_timezone=local_timezone(),
        now=datetime.now(timezone.utc),
    )


def _list_event_types():
    return _list_event_types_action(event_type_class=EventType, render=_render)


def _create_event_type(form: dict[str, str]):
    return _create_event_type_action(form, event_type_class=EventType, render=_render)


def _view_event_type(event_type_id: str):
    return _view_event_type_action(event_type_id, event_type_class=EventType, render=_render)


def _update_event_type(event_type_id: str, form: dict[str, str]):
    return _update_event_type_action(
        event_type_id,
        form,
        event_type_class=EventType,
        render=_render,
    )


def _booking_availability(event_type_id: str | None):
    return _booking_availability_action(
        event_type_id,
        event_class=Event,
        event_type_class=EventType,
        render=_render,
        local_timezone=local_timezone(),
        now=datetime.now(timezone.utc),
    )


def _booking_hold(form: dict[str, str]):
    return _booking_hold_action(
        form,
        event_class=Event,
        render=_render,
        local_timezone=local_timezone(),
        now=datetime.now(timezone.utc),
    )


def _booking_contact(form: dict[str, str]):
    return _booking_contact_action(
        form,
        event_class=Event,
        render=_render,
        payment_provider=_payment_provider,
        publishable_key=stripe_publishable_key(),
        now=datetime.now(timezone.utc),
    )


def _booking_complete(form: dict[str, str]):
    return _booking_complete_action(
        form,
        event_class=Event,
        render=_render,
        payment_provider=_payment_provider,
        now=datetime.now(timezone.utc),
    )


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
        current_user_roles=_auth_module.current_user_roles(environ),
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
