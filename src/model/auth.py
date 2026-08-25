from __future__ import annotations

from datetime import datetime, timezone

import mongoengine

from . import mongoengine_alias


class Fido2Credential(mongoengine.EmbeddedDocument):
    """A WebAuthn credential registered for a ticketing user."""

    id = mongoengine.BinaryField(required=True, primary_key=True, db_field="_id")
    data = mongoengine.BinaryField(required=True)
    dt = mongoengine.DateTimeField(required=True, default=lambda: datetime.now(timezone.utc))


class GoogleCredential(mongoengine.EmbeddedDocument):
    """A Google account allowed to authenticate as a ticketing user."""

    email = mongoengine.StringField(required=True, unique=True)


class User(mongoengine.Document):
    """A user and their registered authentication credentials."""

    id = mongoengine.StringField(primary_key=True)
    fido2_credentials = mongoengine.EmbeddedDocumentListField(Fido2Credential, default=list)
    google_credentials = mongoengine.EmbeddedDocumentListField(GoogleCredential, default=list)

    meta = {
        "db_alias": mongoengine_alias,
        "indexes": [
            {"fields": ["fido2_credentials._id"], "unique": True},
            {"fields": ["google_credentials.email"], "unique": True},
        ],
    }
