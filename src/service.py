from __future__ import annotations

import io
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Callable, Iterable
from urllib.parse import parse_qs

from jinja2 import Environment, PackageLoader, select_autoescape
from mongoengine.connection import _connections
from mongoengine.errors import NotUniqueError
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from . import PDF, Ticket
from .model import mongoengine_alias


MAX_TICKETS = 1000
MAX_CODE_ATTEMPTS = 20
MAX_FORM_BYTES = 1024 * 1024
CODE_LENGTH = 16
CODE_ALPHABET = "".join(chr(code_point) for code_point in range(33, 127))

_templates = Environment(
    loader=PackageLoader("dropzone_ticketing", "templates"),
    autoescape=select_autoescape(["html"]),
)
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
            mongo_client = MongoClient(os.environ["MONGODB_CONNECTION_STR"])
            _connections[mongoengine_alias] = mongo_client
            _storage_connected = True


def _render(template_name: str, status: HTTPStatus = HTTPStatus.OK, **context: object):
    body = _templates.get_template(template_name).render(**context).encode("utf-8")
    return status, [("Content-Type", "text/html; charset=utf-8")], body


def _read_form(environ: dict) -> dict[str, str]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError as error:
        raise ValueError("Invalid request body length.") from error
    if length < 0 or length > MAX_FORM_BYTES:
        raise ValueError("Request body is too large.")

    raw_body = environ["wsgi.input"].read(length)
    try:
        values = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as error:
        raise ValueError("Form data must be UTF-8 encoded.") from error
    return {name: entries[0] for name, entries in values.items()}


def _issue(form: dict[str, str]):
    owner = form.get("owner", "").strip()
    if not owner:
        return _render("issue.html", HTTPStatus.BAD_REQUEST, error="Owner is required.", owner=owner)

    try:
        count = int(form.get("count", ""))
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_TICKETS:
        return _render(
            "issue.html",
            HTTPStatus.BAD_REQUEST,
            error=f"Count must be between 1 and {MAX_TICKETS}.",
            owner=owner,
        )

    tickets = []
    for _ in range(count):
        for _attempt in range(MAX_CODE_ATTEMPTS):
            ticket = Ticket(code=generate_code(), owner=owner)
            try:
                ticket.save()
            except (NotUniqueError, DuplicateKeyError):
                continue
            tickets.append(ticket)
            break
        else:
            raise RuntimeError("Could not generate a unique ticket code.")

    return _render("issued.html", owner=owner, tickets=tickets)


def _redeem(form: dict[str, str]):
    codes = split_codes(form.get("codes", ""))
    if not codes:
        return _render(
            "redeem.html",
            HTTPStatus.BAD_REQUEST,
            error="Enter at least one ticket code.",
        )

    results = []
    for code in codes:
        ticket = Ticket.objects(code=code).first()
        if ticket is None:
            results.append({"code": code, "result": "not found"})
        elif ticket.redeemed is not None:
            results.append(
                {
                    "code": code,
                    "result": "already redeemed",
                    "redeemed": ticket.redeemed,
                }
            )
        else:
            ticket.redeemed = datetime.now(timezone.utc)
            ticket.save()
            results.append({"code": code, "result": "redeemed OK"})

    return _render("redeemed.html", results=results)


def _safe_filename(owner: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", owner).strip(".-_")
    return f"tickets-{component or 'owner'}.pdf"


def _print_tickets(owner: str):
    owner = owner.strip()
    if not owner:
        return _render("print.html", HTTPStatus.BAD_REQUEST, error="Owner is required.")

    tickets = list(Ticket.objects(owner=owner, redeemed=None))
    if not tickets:
        return _render("print.html", owner=owner, message="No active tickets found for this owner.")

    output = io.BytesIO()
    pdf = PDF(output)
    for ticket in tickets:
        pdf.append(ticket)
    pdf.render()
    body = output.getvalue()
    return (
        HTTPStatus.OK,
        [
            ("Content-Type", "application/pdf"),
            ("Content-Disposition", f'attachment; filename="{_safe_filename(owner)}"'),
        ],
        body,
    )


def _method_not_allowed(allowed: Iterable[str]):
    methods = ", ".join(allowed)
    status, headers, body = _render(
        "error.html",
        HTTPStatus.METHOD_NOT_ALLOWED,
        message=f"Method not allowed. Use {methods}.",
    )
    headers.append(("Allow", methods))
    return status, headers, body


def _dispatch(environ: dict):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()

    if path == "/":
        if method != "GET":
            return _method_not_allowed(["GET"])
        return _render("index.html")

    if path == "/issue":
        if method == "GET":
            return _render("issue.html")
        if method == "POST":
            return _issue(_read_form(environ))
        return _method_not_allowed(["GET", "POST"])

    if path == "/redeem":
        if method == "GET":
            return _render("redeem.html")
        if method == "POST":
            return _redeem(_read_form(environ))
        return _method_not_allowed(["GET", "POST"])

    if path == "/print":
        if method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            if "owner" not in query:
                return _render("print.html")
            return _print_tickets(query["owner"][0])
        if method == "POST":
            return _print_tickets(_read_form(environ).get("owner", ""))
        return _method_not_allowed(["GET", "POST"])

    return _render("error.html", HTTPStatus.NOT_FOUND, message="Page not found.")


def application(environ: dict, start_response: Callable):
    """Serve the ticket issuing, redemption, and printing workflows."""
    try:
        _ensure_storage()
        status, headers, body = _dispatch(environ)
    except ValueError as error:
        status, headers, body = _render("error.html", HTTPStatus.BAD_REQUEST, message=str(error))
    except Exception:
        status, headers, body = _render(
            "error.html",
            HTTPStatus.INTERNAL_SERVER_ERROR,
            message="The request could not be completed.",
        )

    headers.append(("Content-Length", str(len(body))))
    start_response(f"{status.value} {status.phrase}", headers)
    return [body]
