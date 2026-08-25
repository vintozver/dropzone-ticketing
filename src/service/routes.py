from __future__ import annotations

import json
from http import HTTPStatus
from urllib.parse import parse_qs

from . import auth, google, register
from .config import authn_config
from .http import method_not_allowed, render

def dispatch(environ: dict, handlers):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    registration_mode = authn_config().register

    if registration_mode and path != "/register":
        return render(
            "error.html",
            HTTPStatus.FORBIDDEN,
            message="Application is running in registration-only mode.",
        )

    if path == "/authn":
        if method == "GET":
            return auth.begin_authn(environ)
        if method == "POST":
            return auth.complete_authn(environ)
        return method_not_allowed(["GET", "POST"])

    if path == "/authn/register":
        if method == "POST":
            return auth.complete_authn_register(environ)
        return method_not_allowed(["POST"])

    if path == "/authn/display-name":
        if method == "POST":
            return auth.update_display_name(environ)
        return method_not_allowed(["POST"])

    if path == "/authn/fido2/remove":
        if method == "POST":
            return auth.remove_fido2_credential(environ)
        return method_not_allowed(["POST"])

    if path == "/authn/google":
        if method == "GET":
            return google.begin(environ)
        return method_not_allowed(["GET"])

    if path == "/authn/google/callback":
        if method == "GET":
            return google.complete(environ)
        return method_not_allowed(["GET"])

    if path == "/authn/google/remove":
        if method == "POST":
            return google.remove(environ)
        return method_not_allowed(["POST"])

    if path == "/register":
        if method == "GET":
            return register.begin_register(environ)
        if method == "POST":
            return register.complete_register(environ)
        return method_not_allowed(["GET", "POST"])

    if path == "/logout":
        if method != "POST":
            return method_not_allowed(["POST"])
        return auth.logout()

    if path == "/":
        if method != "GET":
            return method_not_allowed(["GET"])
        return render("index.html", authenticated=handlers._is_authenticated(environ))

    if path == "/issue":
        if method not in {"GET", "POST"}:
            return method_not_allowed(["GET", "POST"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        if method == "GET":
            return render("issue.html")
        return handlers._issue(handlers._read_form(environ), handlers._current_user_ref(environ))

    if path == "/redeem":
        if method not in {"GET", "POST"}:
            return method_not_allowed(["GET", "POST"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        if method == "GET":
            return render("redeem.html")
        return handlers._redeem(handlers._read_form(environ), handlers._current_user_ref(environ))

    if path.startswith("/tickets/"):
        if method != "GET":
            return method_not_allowed(["GET"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        return handlers._view_ticket(path.removeprefix("/tickets/"))

    if path == "/tickets":
        if method != "GET":
            return method_not_allowed(["GET"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        user_id = query.get("user_id", [None])[0]
        display_name = query.get("display_name", [None])[0]
        if user_id is None and display_name is None:
            return handlers._view_owners()
        return handlers._view_owner_tickets(user_id, display_name)

    if path == "/users/search":
        if method != "GET":
            return method_not_allowed(["GET"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).get("q", [""])[0]
        return HTTPStatus.OK, [("Content-Type", "application/json; charset=utf-8")], json.dumps(
            handlers._search_users(query)
        ).encode("utf-8")

    if path == "/reports/redeemed":
        if method != "GET":
            return method_not_allowed(["GET"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        return handlers._view_redeemed_tickets()

    if path == "/reports/issued":
        if method != "GET":
            return method_not_allowed(["GET"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        return handlers._view_issued_tickets()

    if path == "/print":
        if method != "GET":
            return method_not_allowed(["GET"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        user_id = query["user_id"][0] if "user_id" in query else None
        display_name = query["display_name"][0] if "display_name" in query else None
        return handlers._print_tickets(query.get("id", []), user_id, display_name)

    return render("error.html", HTTPStatus.NOT_FOUND, message="Page not found.")
