from __future__ import annotations

from datetime import datetime, timezone

import mongoengine

from . import mongoengine_alias
from .ticket import UserRef


class EventBooking(mongoengine.EmbeddedDocument):
    """A user's booking for an event."""

    user = mongoengine.EmbeddedDocumentField(UserRef, required=True)
    dt = mongoengine.DateTimeField(
        required=True,
        default=lambda: datetime.now(timezone.utc),
    )


class Event(mongoengine.Document):
    """An event with a limited number of bookable places."""

    name = mongoengine.StringField(required=True)
    description = mongoengine.StringField(required=False)
    starts = mongoengine.DateTimeField(required=True)
    capacity = mongoengine.IntField(required=True, min_value=1)
    created_by = mongoengine.EmbeddedDocumentField(UserRef, required=True)
    bookings = mongoengine.EmbeddedDocumentListField(EventBooking, default=list)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "event",
        "indexes": ["starts", "bookings.user.id"],
    }
