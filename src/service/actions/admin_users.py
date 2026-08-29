from __future__ import annotations

import json
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId
from mongoengine.errors import NotUniqueError

from ...model.auth import USER_ROLES


_IDENTITY_TYPES = {"email", "google", "microsoft"}


def _render_new_user(render, status=HTTPStatus.OK, **context):
    return render("admin_user_new.html", status, **context)


def new_user(*, render):
    return _render_new_user(render)


def admin_index(*, render):
    return render("admin.html")


def list_users(*, user_class, render):
    users = [
        {
            "id": user.id,
            "display_name": user.display_name,
            "roles": user.roles,
            "email": user.email,
            "google_credentials": [credential.email for credential in user.google_credentials],
            "microsoft_credentials": [credential.email for credential in user.microsoft_credentials],
            "fido2_credentials": [credential.id.hex() for credential in user.fido2_credentials],
        }
        for user in user_class.objects().order_by("display_name", "id")
    ]
    return render(
        "admin_user_list.html",
        users=users,
    )


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
    if role not in USER_ROLES:
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
    except NotUniqueError:
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


def _find_user(user_id, *, user_class):
    try:
        object_id = ObjectId(user_id)
    except (InvalidId, TypeError):
        return None
    return user_class.objects(id=object_id).first()


def view_user(user_id, *, user_class, render):
    user = _find_user(user_id, user_class=user_class)
    if user is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="User not found.")

    fido2_credentials = [
        {
            "encoded_id": credential.id.hex(),
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


def update_user(user_id, form, *, user_class, render):
    user = _find_user(user_id, user_class=user_class)
    if user is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="User not found.")

    action = form.get("action", "")
    if action == "update":
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().casefold()
        role = form.get("role", "")
        if not name or len(name) > 200:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="A valid name is required.")
        if email and ("@" not in email or len(email) > 320):
            return render("error.html", HTTPStatus.BAD_REQUEST, message="A valid email address is required.")
        if role not in USER_ROLES:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a role.")
        user.display_name = name
        user.email = email or None
        user.roles = [role]
    elif action == "remove_google":
        email = form.get("credential", "").strip().casefold()
        user.google_credentials = [
            credential for credential in user.google_credentials if credential.email.casefold() != email
        ]
    elif action == "remove_microsoft":
        email = form.get("credential", "").strip().casefold()
        user.microsoft_credentials = [
            credential for credential in user.microsoft_credentials if credential.email.casefold() != email
        ]
    elif action == "remove_fido2":
        try:
            credential_id = bytes.fromhex(form.get("credential", ""))
        except ValueError:
            return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid credential.")
        user.fido2_credentials = [
            credential for credential in user.fido2_credentials if credential.id != credential_id
        ]
    else:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid action.")

    try:
        user.save()
    except NotUniqueError:
        return render(
            "error.html",
            HTTPStatus.CONFLICT,
            message="A user with this email address already exists.",
        )
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/user/view/{user.id}")], b""
