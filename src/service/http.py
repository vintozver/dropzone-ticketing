from __future__ import annotations

import traceback
from http import HTTPStatus
from typing import Callable
from urllib.parse import parse_qs

from jinja2 import Environment, PackageLoader, select_autoescape

from .config import MAX_FORM_BYTES

_templates = Environment(
    loader=PackageLoader("dropzone_ticketing", "templates"),
    autoescape=select_autoescape(["html"]),
)


def render(template_name: str, status: HTTPStatus = HTTPStatus.OK, **context: object):
    body = _templates.get_template(template_name).render(**context).encode("utf-8")
    return status, [("Content-Type", "text/html; charset=utf-8")], body


def error(status: HTTPStatus, message: str, trace: str = ""):
    return render("error.html", status, message=message, trace=trace)


def read_form(environ: dict) -> dict[str, str]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError as error_:  # pragma: no cover - exercised through application error path
        raise ValueError("Invalid request body length.") from error_
    if length < 0 or length > MAX_FORM_BYTES:
        raise ValueError("Request body is too large.")

    raw_body = environ["wsgi.input"].read(length)
    try:
        values = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as error_:
        raise ValueError("Form data must be UTF-8 encoded.") from error_
    return {name: entries[0] for name, entries in values.items()}


def method_not_allowed(allowed):
    methods = ", ".join(allowed)
    status, headers, body = render(
        "error.html",
        HTTPStatus.METHOD_NOT_ALLOWED,
        message=f"Method not allowed. Use {methods}.",
    )
    headers.append(("Allow", methods))
    return status, headers, body


def response_with_length(response, start_response: Callable):
    status, headers, body = response
    headers.append(("Content-Length", str(len(body))))
    start_response(f"{status.value} {status.phrase}", headers)
    return [body]


def exception_response(exc: Exception):
    return render(
        "error.html",
        HTTPStatus.INTERNAL_SERVER_ERROR,
        message="The request could not be completed.",
        trace="".join(traceback.format_exception(exc)),
    )
