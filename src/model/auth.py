from __future__ import annotations

import mongoengine

from . import mongoengine_alias


class Fido2Credential(mongoengine.EmbeddedDocument):
    """A WebAuthn credential registered for a ticketing user."""

    credential_id = mongoengine.BinaryField(required=True)
    credential_data = mongoengine.BinaryField(required=True)


class User(mongoengine.Document):
    """A user and their registered WebAuthn credentials."""

    id = mongoengine.StringField(primary_key=True)
    fido2_credentials = mongoengine.EmbeddedDocumentListField(Fido2Credential, default=list)

    meta = {
        "db_alias": mongoengine_alias,
        "indexes": [{"fields": ["fido2_credentials.credential_id"], "unique": True}],
    }
