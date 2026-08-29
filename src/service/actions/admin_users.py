from __future__ import annotations

import json
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from mongoengine.errors import NotUniqueError, ValidationError


_IDENTITY_TYPES = {"email", "google", "microsoft"}
_ROLES = {"user", "admin"}


def _render_new_user(render, status=HTTPStatus.OK, **context):
    return render(
        "admin_user_new.html",
        status,
        identity_types=(
            ("email", "Regular email with code validation"),
            ("google", "Google"),
            ("microsoft", "Microsoft"),
        ),
        roles=(("user", "User"), ("admin", "Administrator")),
        **context,
    )


def new_user(*, render):
    return _render_new_user(render)


def create_user(form, *, user_class, google_credential_class, microsoft_credential_class, render):
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().casefold()
    identity_type = form.get("identity_type", "")
    role = form.get("role", "")

    if not name:
        return _render_new_user(
            render,
            HTTPStatus.BAD_REQUEST,
            error="Name is required.",
            name=name,
            email=email,
            identity_type=identity_type,
            role=role,
        )
    if len(name) > 200:
        return _render_new_user(
            render,
            HTTPStatus.BAD_REQUEST,
            error="Name is too long.",
            name=name,
            email=email,
            identity_type=identity_type,
            role=role,
        )
    if not email or "@" not in email or len(email) > 320:
        return _render_new_user(
            render,
            HTTPStatus.BAD_REQUEST,
            error="A valid email address is required.",
            name=name,
            email=email,
            identity_type=identity_type,
            role=role,
        )
    if identity_type not in _IDENTITY_TYPES:
        return _render_new_user(
            render,
            HTTPStatus.BAD_REQUEST,
            error="Choose how the email will be used.",
            name=name,
            email=email,
            identity_type=identity_type,
            role=role,
        )
    if role not in _ROLES:
        return _render_new_user(
            render,
            HTTPStatus.BAD_REQUEST,
            error="Choose a role.",
            name=name,
            email=email,
            identity_type=identity_type,
            role=role,
        )

    user = user_class(display_name=name, roles=[role])
    if identity_type == "email":
        user.email = email
    elif identity_type == "google":
        user.google_credentials.append(google_credential_class(email=email))
    else:
        user.microsoft_credentials.append(microsoft_credential_class(email=email))

    try:
        user.save()
    except (NotUniqueError, ValidationError):
        return _render_new_user(
            render,
            HTTPStatus.CONFLICT,
            error="A user with this email credential already exists.",
            name=name,
            email=email,
            identity_type=identity_type,
            role=role,
        )
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/user/view/{user.id}")], b""


def view_user(user_id, *, user_class, render):
    try:
        object_id = ObjectId(user_id)
    except (InvalidId, TypeError):
        return render("error.html", HTTPStatus.NOT_FOUND, message="User not found.")
    user = user_class.objects(id=object_id).first()
    if user is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="User not found.")

    fido2_credentials = [
        {
            "id": credential.id.hex(),
            "dt": credential.dt,
            "aaguid": credential.attestation_aaguid.hex() if credential.attestation_aaguid else "",
            "extensions": (
                json.dumps(credential.extensions, indent=2, sort_keys=True, default=str)
                if credential.extensions
                else ""
            ),
        }
        for credential in user.fido2_credentials
    ]
    return render(
        "admin_user_view.html",
        user=user,
        fido2_credentials=fido2_credentials,
    )
