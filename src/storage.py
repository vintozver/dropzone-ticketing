from __future__ import annotations

from typing import Any

import mongoengine
import pymongo

from .model import mongoengine_alias


DEFAULT_DATABASE = "dropzone_ticketing"
DEFAULT_HOST = "mongodb://localhost:27017"


def connect_storage(
    db: str = DEFAULT_DATABASE,
    host: str = DEFAULT_HOST,
    **kwargs: Any,
) -> None:
    """Connect to the MongoDB instance backing the ticket storage.

    Thin wrapper around :func:`mongoengine.connect` so that callers do not have
    to import mongoengine themselves. The connection is registered under the
    :data:`~dropzone_ticketing.model.mongoengine_alias` used by the documents.
    """
    mongoengine.connect(db=db, host=host, alias=mongoengine_alias, **kwargs)


def get_client() -> pymongo.MongoClient:
    """Return the underlying pymongo client of the established connection."""
    return mongoengine.get_connection(mongoengine_alias)
