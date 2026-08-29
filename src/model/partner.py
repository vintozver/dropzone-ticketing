from __future__ import annotations

import mongoengine

from . import mongoengine_alias


class PartnerKeyItem(mongoengine.EmbeddedDocument):
    """A signing key or certificate belonging to a partner."""

    id = mongoengine.StringField(required=True)
    pub = mongoengine.BinaryField(required=False)
    crt = mongoengine.BinaryField(required=False)


class Partner(mongoengine.Document):
    """An external ticketing integration."""

    display_name = mongoengine.StringField(required=True)
    keyset = mongoengine.EmbeddedDocumentListField(PartnerKeyItem, default=list)

    meta = {
        "db_alias": mongoengine_alias,
        "indexes": [{"fields": ["display_name"]}],
    }
