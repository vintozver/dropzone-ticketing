from __future__ import annotations

from http import HTTPStatus

from . import report, ticket, user
from ._shared import _json_response, _verify

__all__ = ["dispatch"]


def dispatch(environ: dict):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "")
    try:
        partner, claims = _verify(environ)
        response = report.dispatch(method, path, partner)
        if response is not None:
            return response
        response = ticket.dispatch(method, path, environ, claims, partner)
        if response is not None:
            return response
        response = user.dispatch(method, path, environ, claims, partner)
        if response is not None:
            return response
        return _json_response(HTTPStatus.NOT_FOUND, {"error": "Not found."})
    except PermissionError as exc:
        return _json_response(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
    except ValueError as exc:
        return _json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
