from __future__ import annotations

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
        "collection": "tickets",
        "indexes": ["code"],
    }
