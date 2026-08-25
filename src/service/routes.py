from __future__ import annotations

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
        return handlers._issue(handlers._read_form(environ), handlers._current_user_id(environ))

    if path == "/redeem":
        if method not in {"GET", "POST"}:
            return method_not_allowed(["GET", "POST"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        if method == "GET":
            return render("redeem.html")
        return handlers._redeem(handlers._read_form(environ), handlers._current_user_id(environ))

    if path == "/tickets":
        if method != "GET":
            return method_not_allowed(["GET"])
        auth_response = handlers._require_auth(environ)
        if auth_response is not None:
            return auth_response
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        if "owner" not in query:
            return handlers._view_owners()
        return handlers._view_owner_tickets(query["owner"][0])

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
        owner = query["owner"][0] if "owner" in query else None
        return handlers._print_tickets(query.get("id", []), owner)

    return render("error.html", HTTPStatus.NOT_FOUND, message="Page not found.")
