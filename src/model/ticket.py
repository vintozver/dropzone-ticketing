from __future__ import annotations

from datetime import datetime

import mongoengine

from . import mongoengine_alias


class Redemption(mongoengine.EmbeddedDocument):
    """Details recorded when a ticket is redeemed."""

    dt = mongoengine.DateTimeField(required=True)
    reason = mongoengine.StringField(required=False)


class Ticket(mongoengine.Document):
    """A ticket record persisted in MongoDB.

    The automatically assigned ``id`` (``ObjectIdField``) is the primary key and
    also carries the issue timestamp via
    :attr:`~bson.objectid.ObjectId.generation_time`.

    ``redeemed`` holds redemption details and is unset while the ticket has not
    been redeemed yet.
    """

    code = mongoengine.StringField(required=True, unique=True)
    owner = mongoengine.StringField(required=True)
    payment = mongoengine.StringField(required=True)
    purpose = mongoengine.StringField(required=True)
    redeemed = mongoengine.EmbeddedDocumentField(Redemption, required=False)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "tickets",
        "indexes": ["code"],
    }

    def issued_utc(self) -> datetime:
        """Return the UTC issue timestamp derived from the ticket's ``id``.

        Raises:
            ValueError: if the ticket has no ``id`` yet, so no issue timestamp
                can be derived.
        """
        if self.id is None:
            raise ValueError("ticket has no id; assign or save one before calling issued_utc()")
        return self.id.generation_time
