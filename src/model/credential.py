from __future__ import annotations

import mongoengine

from . import mongoengine_alias


class Fido2Credential(mongoengine.Document):
    """A WebAuthn credential registered for a ticketing user."""

    username = mongoengine.StringField(required=True)
    credential_id = mongoengine.BinaryField(required=True, unique=True)
    credential_data = mongoengine.BinaryField(required=True)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "fido2_credentials",
        "indexes": ["username", "credential_id"],
    }
