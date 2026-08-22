from __future__ import annotations

from datetime import datetime
from typing import Any

import mongoengine
import pymongo


DEFAULT_DATABASE = "dropzone_ticketing"
DEFAULT_HOST = "mongodb://localhost:27017"


def connect_storage(
    db: str = DEFAULT_DATABASE,
    host: str = DEFAULT_HOST,
    **kwargs: Any,
) -> None:
    """Connect to the MongoDB instance backing the ticket storage.

    Thin wrapper around :func:`mongoengine.connect` so that callers do not have
    to import mongoengine themselves.
    """
    mongoengine.connect(db=db, host=host, **kwargs)


def get_client(alias: str = mongoengine.DEFAULT_CONNECTION_NAME) -> pymongo.MongoClient:
    """Return the underlying pymongo client of an established connection."""
    return mongoengine.get_connection(alias)


class Ticket(mongoengine.Document):
    """A ticket record persisted in MongoDB.

    Mirrors the concepts of the rendering ticket
    (:class:`dropzone_ticketing.ticket.Ticket`) and adds the redemption state
    which only makes sense for stored tickets.
    """

    identifier = mongoengine.StringField(required=True, unique=True)
    code = mongoengine.StringField(required=True)
    date_issued = mongoengine.DateTimeField(required=True)
    owner = mongoengine.StringField(required=True)
    redeemed = mongoengine.BooleanField(default=False)
    redeemed_at = mongoengine.DateTimeField(null=True)

    meta = {
        "collection": "tickets",
        "indexes": ["identifier"],
    }

    def __str__(self) -> str:
        return f"Ticket {self.identifier} ({self.owner})"

    def redeem(self, when: datetime) -> None:
        """Mark the ticket as redeemed at the given UTC timestamp.

        Only the in-memory document is updated; call :meth:`save` to persist
        the change.
        """
        self.redeemed = True
        self.redeemed_at = when
