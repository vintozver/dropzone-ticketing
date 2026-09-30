from __future__ import annotations

from datetime import datetime

import mongoengine

from . import mongoengine_alias
from .ticket import UserRef


class Customer(mongoengine.EmbeddedDocument):
    name = mongoengine.StringField(required=True)
    email = mongoengine.EmailField(required=True)
    phone = mongoengine.StringField(required=True)


class Payment(mongoengine.EmbeddedDocument):
    provider = mongoengine.StringField(required=True)
    reference = mongoengine.StringField(required=True)
    amount = mongoengine.IntField(required=True, min_value=0)
    currency = mongoengine.StringField(required=True)
    paid_at = mongoengine.DateTimeField(required=True)


class EventHistory(mongoengine.EmbeddedDocument):
    dt = mongoengine.DateTimeField(required=True)
    by = mongoengine.EmbeddedDocumentField(UserRef, required=True)
    action = mongoengine.StringField(required=True)
    comment = mongoengine.StringField(required=False)


class EventType(mongoengine.Document):
    name = mongoengine.StringField(required=True, unique=True)
    description = mongoengine.StringField(required=False)
    price = mongoengine.IntField(required=True, min_value=0)
    currency = mongoengine.StringField(required=True, min_length=3, max_length=3)
    active = mongoengine.BooleanField(required=True, default=True)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "event_type",
        "indexes": ["name", "active"],
    }


class Event(mongoengine.Document):
    dt = mongoengine.DateTimeField(required=True)
    duration_minutes = mongoengine.IntField(required=True, min_value=1)
    event_type = mongoengine.ReferenceField(EventType, required=True)
    price = mongoengine.IntField(required=True, min_value=0)
    currency = mongoengine.StringField(required=True, min_length=3, max_length=3)
    active = mongoengine.BooleanField(required=True, default=True)
    customer = mongoengine.EmbeddedDocumentField(Customer, required=False)
    payment = mongoengine.EmbeddedDocumentField(Payment, required=False)
    checkout_customer = mongoengine.EmbeddedDocumentField(Customer, required=False)
    payment_intent = mongoengine.StringField(required=False)
    reservation_token = mongoengine.StringField(required=False)
    reservation_expires_at = mongoengine.DateTimeField(required=False)
    history = mongoengine.EmbeddedDocumentListField(EventHistory, default=list)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "event",
        "indexes": [
            "dt",
            "event_type",
            "customer",
            "reservation_expires_at",
            {"fields": ["event_type", "dt"]},
        ],
    }

    def is_reserved(self, now: datetime) -> bool:
        return bool(
            self.customer is None
            and self.reservation_token
            and self.reservation_expires_at
            and self.reservation_expires_at > now
        )
