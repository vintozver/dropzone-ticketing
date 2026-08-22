from __future__ import annotations

import mongoengine

from . import mongoengine_alias


class Ticket(mongoengine.Document):
    """A ticket record persisted in MongoDB.

    The automatically assigned ``id`` (``ObjectIdField``) is the primary key and
    also carries the issue timestamp via
    :attr:`~bson.objectid.ObjectId.generation_time`.

    ``redeemed`` holds the redemption timestamp and is unset while the ticket
    has not been redeemed yet.
    """

    code = mongoengine.StringField(required=True, unique=True)
    owner = mongoengine.StringField(required=True)
    redeemed = mongoengine.DateTimeField(required=False)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "tickets",
        "indexes": ["code"],
    }
