from __future__ import annotations

from datetime import datetime, timezone

import mongoengine

from . import mongoengine_alias


class Fido2Credential(mongoengine.EmbeddedDocument):
    """A WebAuthn credential registered for a ticketing user."""

    id = mongoengine.BinaryField(required=True, primary_key=True, db_field="_id")
    data = mongoengine.BinaryField(required=True)
    attestation_aaguid = mongoengine.BinaryField(required=False)
    extensions = mongoengine.DictField(required=False)
    dt = mongoengine.DateTimeField(required=True, default=lambda: datetime.now(timezone.utc))


class GoogleCredential(mongoengine.EmbeddedDocument):
    """A Google account allowed to authenticate as a ticketing user."""

    email = mongoengine.StringField(required=True)


class MicrosoftCredential(mongoengine.EmbeddedDocument):
    """A Microsoft account allowed to authenticate as a ticketing user."""

    email = mongoengine.StringField(required=True)


class EmailAuthentication(mongoengine.EmbeddedDocument):
    """A temporary code for signing in or confirming an email address."""

    email = mongoengine.StringField(required=True)
    code = mongoengine.StringField(required=True)
    purpose = mongoengine.StringField(required=True, default="signin")
    issued = mongoengine.DateTimeField(required=True, default=lambda: datetime.now(timezone.utc))


class User(mongoengine.Document):
    """A user and their registered authentication credentials."""

    id = mongoengine.ObjectIdField(primary_key=True)
    display_name = mongoengine.StringField(required=False)
    email = mongoengine.StringField(required=False, unique=True, sparse=True)
    email_authentication = mongoengine.EmbeddedDocumentField(EmailAuthentication, required=False)
    fido2_credentials = mongoengine.EmbeddedDocumentListField(Fido2Credential, default=list)
    google_credentials = mongoengine.EmbeddedDocumentListField(GoogleCredential, default=list)
    microsoft_credentials = mongoengine.EmbeddedDocumentListField(MicrosoftCredential, default=list)
    partner_uid_map = mongoengine.DictField(default=dict)

    meta = {
        "db_alias": mongoengine_alias,
        "indexes": [
            {"fields": ["display_name"]},
            {"fields": ["fido2_credentials._id"], "unique": True},
            {"fields": ["google_credentials.email"], "unique": True, "sparse": True},
            {"fields": ["microsoft_credentials.email"], "unique": True, "sparse": True},
        ],
    }
