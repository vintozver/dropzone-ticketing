from __future__ import annotations

from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId

from ...model.auth import User
from ._shared import _json_response, _method_not_allowed, _payload


def dispatch(method: str, path: str, environ: dict, claims: dict, partner):
    """Handle `/api/user*` endpoints. Returns None if `path` is not handled here."""
    if path == "/api/user/list":
        if method != "GET":
            return _method_not_allowed(["GET"])
        return _json_response(HTTPStatus.OK, _list_users(partner))
    if path == "/api/user":
        if method != "PATCH":
            return _method_not_allowed(["PATCH"])
        return _update_user(environ, claims, partner)
    return None


def _list_users(partner):
    users = []
    users_query = User.objects.aggregate(
        [{
            "$project": {
                "_id": 1,
                "display_name": 1,
                "email": 1,
                "external_id": {"$ifNull": [f"$partner_uid_map.{partner.id}", None]},
            }
        }]
    )
    for user in users_query:
        users.append({"external_id": user["external_id"], "internal_id": str(user["_id"]),
                      "display_name": user.get("display_name"), "email": user.get("email")})
    return users


def _update_user(environ: dict, claims: dict, partner):
    values = _payload(environ, claims, ("internal_id", "external_id"))
    internal_id, external_id = values.get("internal_id"), values.get("external_id")
    if not isinstance(internal_id, str) or not isinstance(external_id, str):
        raise ValueError("internal_id and external_id are required.")
    try:
        user = User.objects(id=ObjectId(internal_id)).first()
    except InvalidId:
        user = None
    if user is None:
        return _json_response(HTTPStatus.NOT_FOUND, {"error": "User not found."})
    User.objects(id=user.id).update_one(
        **{"set__partner_uid_map__" + str(partner.id): external_id}
    )
    return _json_response(HTTPStatus.OK, {"internal_id": internal_id, "external_id": external_id})
