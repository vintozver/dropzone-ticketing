from __future__ import annotations

import uuid
from datetime import datetime

import mongoengine

from . import mongoengine_alias
from .ticket import UserRef


class Customer(mongoengine.EmbeddedDocument):
    name = mongoengine.StringField(required=True)
    email = mongoengine.EmailField(required=True)
    phone = mongoengine.StringField(required=False)


class Payment(mongoengine.EmbeddedDocument):
    provider = mongoengine.StringField(required=True)
    reference = mongoengine.StringField(required=True)
    amount = mongoengine.IntField(required=True, min_value=0)
    currency = mongoengine.StringField(required=True)
    dt = mongoengine.DateTimeField(required=True)


class EventHistoryItem(mongoengine.EmbeddedDocument):
    dt = mongoengine.DateTimeField(required=True)
    by = mongoengine.EmbeddedDocumentField(UserRef, required=False)
    action = mongoengine.StringField(
        required=True,
        choices=("payment", "refund", "comment", "book", "cancel"),
    )
    comment = mongoengine.StringField(required=False)
    payment = mongoengine.EmbeddedDocumentField(Payment, required=False)


class QuestionResponse(mongoengine.EmbeddedDocument):
    id = mongoengine.UUIDField(
        required=True,
        binary=False,
        default=uuid.uuid4,
    )
    label = mongoengine.StringField(required=True)


class EventQuestion(mongoengine.EmbeddedDocument):
    id = mongoengine.UUIDField(
        required=True,
        binary=False,
        default=uuid.uuid4,
    )
    text = mongoengine.StringField(required=True)
    order = mongoengine.IntField(required=True, default=0)
    multiple = mongoengine.BooleanField(required=True, default=False)
    visible = mongoengine.BooleanField(required=True, default=True)
    responses = mongoengine.EmbeddedDocumentListField(QuestionResponse, default=list)


def _validate_question_ids(questions) -> None:
    question_ids = [question.id for question in questions]
    response_ids = [
        response.id
        for question in questions
        for response in question.responses
    ]
    if len(question_ids) != len(set(question_ids)):
        raise mongoengine.ValidationError("Question identifiers must be unique.")
    if len(response_ids) != len(set(response_ids)):
        raise mongoengine.ValidationError("Response identifiers must be unique.")


class EventType(mongoengine.Document):
    name = mongoengine.StringField(required=True, unique=True)
    description = mongoengine.StringField(required=False)
    price = mongoengine.IntField(required=True, min_value=0)
    currency = mongoengine.StringField(required=True, min_length=3, max_length=3)
    active = mongoengine.BooleanField(required=True, default=True)
    questions = mongoengine.EmbeddedDocumentListField(EventQuestion, default=list)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "event_type",
        "indexes": ["name", "active"],
    }

    def clean(self):
        _validate_question_ids(self.questions)


class Event(mongoengine.Document):
    dt = mongoengine.DateTimeField(required=True)
    duration_minutes = mongoengine.IntField(required=True, min_value=1)
    event_type = mongoengine.ReferenceField(EventType, required=True)
    price = mongoengine.IntField(required=True, min_value=0)
    currency = mongoengine.StringField(required=True, min_length=3, max_length=3)
    active = mongoengine.BooleanField(required=True, default=True)
    customer = mongoengine.EmbeddedDocumentField(Customer, required=False)
    checkout_customer = mongoengine.EmbeddedDocumentField(Customer, required=False)
    questions = mongoengine.EmbeddedDocumentListField(EventQuestion, default=list)
    responses = mongoengine.DictField(
        field=mongoengine.ListField(mongoengine.StringField()),
        default=dict,
    )
    checkout_responses = mongoengine.DictField(
        field=mongoengine.ListField(mongoengine.StringField()),
        default=dict,
    )
    payment_intent = mongoengine.StringField(required=False)
    reservation_token = mongoengine.StringField(required=False)
    reservation_expires_at = mongoengine.DateTimeField(required=False)
    history = mongoengine.EmbeddedDocumentListField(EventHistoryItem, default=list)

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

    def clean(self):
        _validate_question_ids(self.questions)


def migrate_events() -> None:
    collection = Event._get_collection()
    collection.update_many(
        {"starts_at": {"$exists": True}, "dt": {"$exists": False}},
        {"$rename": {"starts_at": "dt"}},
    )
    collection.update_many(
        {"active": {"$exists": False}},
        {"$set": {"active": True}},
    )
    event_type_collection = EventType._get_collection()
    missing_price = {
        "$or": [
            {"price": {"$exists": False}},
            {"currency": {"$exists": False}},
        ]
    }
    for event in collection.find(missing_price, {"_id": 1, "event_type": 1}):
        reference = event.get("event_type")
        event_type_id = getattr(reference, "id", reference)
        event_type = event_type_collection.find_one(
            {"_id": event_type_id},
            {"price": 1, "currency": 1},
        )
        if event_type and "price" in event_type and "currency" in event_type:
            collection.update_one(
                {"_id": event["_id"]},
                {"$set": {"price": event_type["price"], "currency": event_type["currency"]}},
            )
    for event in collection.find(
        {},
        {"_id": 1, "payment": 1, "history": 1},
    ):
        history = event.get("history", [])
        changed = False
        for item in history:
            action = item.get("action")
            if action not in {"payment", "refund", "comment", "book", "cancel"}:
                item["action"] = "comment"
                changed = True
            payment = item.get("payment")
            if payment and "paid_at" in payment and "dt" not in payment:
                payment["dt"] = payment.pop("paid_at")
                changed = True
        legacy_payment = event.get("payment")
        if legacy_payment:
            payment = dict(legacy_payment)
            payment["dt"] = payment.pop("paid_at", event["_id"].generation_time)
            history.append(
                {
                    "dt": payment["dt"],
                    "action": "payment",
                    "payment": payment,
                }
            )
            changed = True
        if changed:
            collection.update_one(
                {"_id": event["_id"]},
                {
                    "$set": {"history": history},
                    "$unset": {"payment": ""},
                },
            )
