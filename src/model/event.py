from __future__ import annotations

from datetime import datetime

import mongoengine
from bson import ObjectId

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
    id = mongoengine.ObjectIdField(
        required=True,
        default=ObjectId,
    )
    label = mongoengine.StringField(required=True)


class Question(mongoengine.EmbeddedDocument):
    id = mongoengine.ObjectIdField(
        required=True,
        default=ObjectId,
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
    order = mongoengine.IntField(required=True, default=0)
    active = mongoengine.BooleanField(required=True, default=True)
    questions = mongoengine.EmbeddedDocumentListField(Question, default=list)

    meta = {
        "db_alias": mongoengine_alias,
        "collection": "event_type",
        "indexes": ["name", "active", "order"],
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
    reservation_nonce = mongoengine.StringField(required=False)
    reservation_slot = mongoengine.IntField(required=False, min_value=0, max_value=2)
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
            {
                "fields": ["reservation_token", "reservation_slot"],
                "unique": True,
                "sparse": True,
            },
        ],
    }

    def is_reserved(self, now: datetime) -> bool:
        return bool(
            self.customer is None
            and self.reservation_token
            and self.reservation_expires_at
            and self.reservation_expires_at > now
        )
