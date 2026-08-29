from __future__ import annotations

from http import HTTPStatus

from . import report, ticket, user
from ._shared import _json_response, _method_not_allowed, _verify

__all__ = ["dispatch"]


def dispatch(environ: dict):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "")
    try:
        partner, claims = _verify(environ)
        if path in {"/api/report/ticket-redeem/today", "/api/report/ticket-redeem/yesterday"}:
            if method != "GET":
                return _method_not_allowed(["GET"])
            yesterday, today, tomorrow = report.day_boundaries()
            start, end = (today, tomorrow) if path.endswith("/today") else (yesterday, today)
            return _json_response(HTTPStatus.OK, report.ticket_redeem_report(partner, start=start, end=end))
        if path == "/api/ticket/redeem":
            if method != "POST":
                return _method_not_allowed(["POST"])
            return ticket.redeem_ticket(environ, claims, partner)
        if path == "/api/user/list":
            if method != "GET":
                return _method_not_allowed(["GET"])
            return _json_response(HTTPStatus.OK, user.list_users(partner))
        if path == "/api/user":
            if method != "PATCH":
                return _method_not_allowed(["PATCH"])
            return user.update_user(environ, claims, partner)
        return _json_response(HTTPStatus.NOT_FOUND, {"error": "Not found."})
    except PermissionError as exc:
        return _json_response(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
    except ValueError as exc:
        return _json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
