from __future__ import annotations

import binascii
import traceback
from http import HTTPStatus
from time import time

from . import auth
from .http import error, read_form


def complete_authn(environ: dict):
    state = auth._authn_state_from_cookie(environ)
    if state is None:
        return auth.authentication_error(environ, "Authentication challenge is missing or expired.")
    form = read_form(environ)
    try:
        response = {
            "id": form.get("id", ""),
            "rawId": form.get("rawId", ""),
            "response": {
                "clientDataJSON": form.get("clientDataJSON", ""),
                "authenticatorData": form.get("authenticatorData", ""),
                "signature": form.get("signature", ""),
                "userHandle": form.get("userHandle", None),
            },
            "type": "public-key",
        }
        credential = auth._find_credential(form.get("rawId", ""))
        if credential is None:
            raise ValueError("Unknown credential.")
        server = auth._server(environ)
        server.authenticate_complete(
            state,
            [auth._credential_data(credential)],
            response=response,
        )
        serial = auth._b64encode(credential.id)
    except (binascii.Error, ValueError):
        return auth.authentication_error(
            environ,
            "FIDO2 authentication failed.",
            traceback.format_exc(),
            auth._safe_return_uri(state.get("return_uri")),
        )
    destination = auth._safe_return_uri(state.get("return_uri")) or "/"
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authenticated.")
    headers = [
        ("Location", destination),
        auth._cookie(auth.AUTHN_SESSION_COOKIE, auth._signed({"serial": serial, "issued": time()})),
        auth._clear_cookie(auth.AUTHN_CHALLENGE_COOKIE, path="/authn"),
    ]
    return status, headers, body
