from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import mongoengine
import stripe
from bson import ObjectId

from dropzone_ticketing.model import mongoengine_alias
from dropzone_ticketing.model.event import (
    Customer,
    Event,
    EventHistory,
    EventType,
    Payment,
    migrate_events,
)
from dropzone_ticketing.service import payment
from dropzone_ticketing.service.actions import admin_events
from dropzone_ticketing.service.actions import booking


class EventModelTest(unittest.TestCase):
    def test_event_type_fields_store_price_in_minor_units(self) -> None:
        fields = EventType._fields
        self.assertIsInstance(fields["name"], mongoengine.StringField)
        self.assertTrue(fields["name"].unique)
        self.assertIsInstance(fields["price"], mongoengine.IntField)
        self.assertEqual(fields["price"].min_value, 0)
        self.assertEqual(fields["currency"].min_length, 3)
        self.assertEqual(fields["currency"].max_length, 3)
        self.assertIs(EventType._meta["db_alias"], mongoengine_alias)

    def test_event_has_booking_and_reservation_fields(self) -> None:
        fields = Event._fields
        self.assertIsInstance(fields["dt"], mongoengine.DateTimeField)
        self.assertIsInstance(fields["duration_minutes"], mongoengine.IntField)
        self.assertIsInstance(fields["event_type"], mongoengine.ReferenceField)
        self.assertIs(fields["event_type"].document_type, EventType)
        self.assertIs(fields["customer"].document_type_obj, Customer)
        self.assertIs(fields["payment"].document_type_obj, Payment)
        self.assertIs(fields["history"].field.document_type_obj, EventHistory)
        self.assertIsInstance(fields["price"], mongoengine.IntField)
        self.assertIsInstance(fields["currency"], mongoengine.StringField)
        self.assertIsInstance(fields["reservation_expires_at"], mongoengine.DateTimeField)
        self.assertIs(Event._meta["db_alias"], mongoengine_alias)

    def test_reservation_is_active_only_before_expiry(self) -> None:
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        event = Event(
            dt=now + timedelta(days=1),
            duration_minutes=60,
            event_type=EventType(name="Tandem", price=10000, currency="EUR"),
            price=12000,
            currency="EUR",
            reservation_token="token",
            reservation_expires_at=now + timedelta(minutes=1),
        )
        self.assertTrue(event.is_reserved(now))
        self.assertFalse(event.is_reserved(now + timedelta(minutes=1)))
        event.customer = Customer(name="Guest", email="guest@example.test", phone="+1")
        self.assertFalse(event.is_reserved(now))

    @patch.object(EventType, "_get_collection")
    @patch.object(Event, "_get_collection")
    def test_migration_renames_time_and_snapshots_default_price(
        self,
        event_collection_factory,
        event_type_collection_factory,
    ) -> None:
        event_id = ObjectId()
        event_type_id = ObjectId()
        event_collection = event_collection_factory.return_value
        event_collection.find.return_value = [
            {"_id": event_id, "event_type": event_type_id}
        ]
        event_type_collection_factory.return_value.find_one.return_value = {
            "price": 10000,
            "currency": "EUR",
        }

        migrate_events()

        event_collection.update_many.assert_any_call(
            {"starts_at": {"$exists": True}, "dt": {"$exists": False}},
            {"$rename": {"starts_at": "dt"}},
        )
        event_collection.update_one.assert_called_once_with(
            {"_id": event_id},
            {"$set": {"price": 10000, "currency": "EUR"}},
        )


class BookingActionTest(unittest.TestCase):
    def test_availability_groups_fourteen_local_days_and_marks_sold_out(self) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event_type = SimpleNamespace(id=ObjectId(), name="Tandem")
        available = SimpleNamespace(
            dt=datetime(2026, 9, 30, 10, tzinfo=timezone.utc),
            customer=None,
            is_reserved=lambda _now: False,
        )
        sold = SimpleNamespace(
            dt=datetime(2026, 10, 1, 10, tzinfo=timezone.utc),
            customer=SimpleNamespace(),
            is_reserved=lambda _now: False,
        )

        event_type_query = MagicMock()
        event_type_query.order_by.return_value = [event_type]
        selected_query = MagicMock()
        selected_query.first.return_value = event_type
        event_type_class = MagicMock()
        event_type_class.objects.side_effect = [event_type_query, selected_query]
        event_query = MagicMock()
        event_query.order_by.return_value = [available, sold]
        event_class = MagicMock()
        event_class.objects.return_value = event_query
        render = MagicMock(return_value="rendered")

        result = booking.availability(
            str(event_type.id),
            event_class=event_class,
            event_type_class=event_type_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=now,
        )

        self.assertEqual(result, "rendered")
        days = render.call_args.kwargs["days"]
        self.assertEqual(len(days), 14)
        self.assertEqual(days[0]["events"], [available])
        self.assertTrue(days[1]["sold_out"])
        self.assertFalse(days[2]["has_events"])

    @patch("dropzone_ticketing.service.actions.booking.secrets.token_urlsafe", return_value="secret")
    def test_hold_is_atomic_and_last_exactly_fifteen_minutes(self, _token) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event = SimpleNamespace(id=ObjectId())
        query = MagicMock()
        query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = query
        render = MagicMock(return_value="rendered")

        result = booking.hold(
            {"event_id": str(event.id)},
            event_class=event_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=now,
        )

        self.assertEqual(result, "rendered")
        self.assertEqual(
            query.modify.call_args.kwargs["set__reservation_expires_at"],
            now + timedelta(minutes=15),
        )
        self.assertEqual(
            query.modify.call_args.kwargs["set__reservation_token"],
            booking._token_hash("secret"),
        )

    def test_hold_conflict_does_not_override_an_existing_reservation(self) -> None:
        query = MagicMock()
        query.modify.return_value = None
        event_class = MagicMock()
        event_class.objects.return_value = query
        render = MagicMock(return_value="conflict")

        result = booking.hold(
            {"event_id": str(ObjectId())},
            event_class=event_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "conflict")
        self.assertEqual(render.call_args.args[1], HTTPStatus.CONFLICT)

    def test_complete_verifies_authorization_then_captures_and_books(self) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event_id = ObjectId()
        customer = Customer(name="Guest", email="guest@example.test", phone="+1")
        event_type = SimpleNamespace(price=12500, currency="EUR")
        event = SimpleNamespace(
            id=event_id,
            event_type=event_type,
            price=12500,
            currency="EUR",
            checkout_customer=customer,
        )
        authorized = SimpleNamespace(id=event_id)
        booked = SimpleNamespace(id=event_id)
        lookup_query = MagicMock()
        lookup_query.first.return_value = event
        authorize_query = MagicMock()
        authorize_query.modify.return_value = authorized
        finish_query = MagicMock()
        finish_query.modify.return_value = booked
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup_query, authorize_query, finish_query]
        token_hash = booking._token_hash("secret")
        provider = MagicMock()
        provider.retrieve_payment_intent.return_value = {
            "id": "pi_1",
            "status": "requires_capture",
            "amount": 12500,
            "currency": "eur",
            "metadata": {"event_id": str(event_id), "reservation_token": token_hash},
        }
        provider.capture_payment_intent.return_value = {
            "status": "succeeded",
            "amount_received": 12500,
            "currency": "eur",
        }
        render = MagicMock(return_value="confirmed")

        result = booking.complete(
            {"event_id": str(event_id), "token": "secret", "payment_intent": "pi_1"},
            event_class=event_class,
            render=render,
            payment_provider=provider,
            now=now,
        )

        self.assertEqual(result, "confirmed")
        provider.capture_payment_intent.assert_called_once_with("pi_1")
        self.assertEqual(authorize_query.modify.call_args.kwargs["set__customer"], customer)
        recorded_payment = finish_query.modify.call_args.kwargs["set__payment"]
        self.assertEqual(recorded_payment.reference, "pi_1")
        self.assertEqual(recorded_payment.amount, 12500)


class StripePaymentTest(unittest.TestCase):
    @patch("dropzone_ticketing.service.payment._client")
    def test_payment_intent_uses_manual_capture_and_idempotency(self, client) -> None:
        create = client.return_value.v1.payment_intents.create
        payment.create_payment_intent(
            amount=5000,
            currency="EUR",
            email="guest@example.test",
            event_id="event",
            reservation_token="reservation-hash",
        )

        values = create.call_args.args[0]
        self.assertEqual(values["capture_method"], "manual")
        self.assertEqual(create.call_args.args[1]["idempotency_key"], "reservation-hash")

    @patch("dropzone_ticketing.service.payment._client")
    def test_refund_uses_payment_intent_and_idempotency(self, client) -> None:
        create = client.return_value.v1.refunds.create
        payment.refund_payment("pi_1")
        self.assertEqual(create.call_args.args[0], {"payment_intent": "pi_1"})
        self.assertEqual(
            create.call_args.args[1]["idempotency_key"],
            "event-cancellation-pi_1",
        )

    def test_stripe_resources_are_converted_to_plain_dictionaries(self) -> None:
        resource = stripe.PaymentIntent.construct_from(
            {
                "id": "pi_1",
                "status": "requires_capture",
                "metadata": {"event_id": "event"},
            },
            None,
        )
        result = payment._call(MagicMock(return_value=resource))
        self.assertEqual(result["status"], "requires_capture")
        self.assertEqual(result["metadata"]["event_id"], "event")


class EventManagementTest(unittest.TestCase):
    def test_create_event_copies_explicit_price_and_records_user_history(self) -> None:
        event_type = SimpleNamespace(id=ObjectId())
        type_query = MagicMock()
        type_query.first.return_value = event_type
        event_type_class = MagicMock()
        event_type_class.objects.return_value = type_query
        event = MagicMock(id=ObjectId())
        event_class = MagicMock(return_value=event)
        user_id = ObjectId()

        status, headers, _body = admin_events.create_event(
            {
                "dt": "2026-10-01T10:30",
                "duration_minutes": "90",
                "event_type": str(event_type.id),
                "price": "199.95",
                "currency": "eur",
                "comment": "Opening slot",
            },
            {"id": user_id, "display_name": "Manager"},
            event_class=event_class,
            event_type_class=event_type_class,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertEqual(headers[0][0], "Location")
        self.assertEqual(event_class.call_args.kwargs["price"], 19995)
        self.assertEqual(event_class.call_args.kwargs["currency"], "EUR")
        history = event_class.call_args.kwargs["history"][0]
        self.assertEqual(history.action, "created")
        self.assertEqual(history.by.id, user_id)
        self.assertEqual(history.by.display_name, "Manager")
        event.save.assert_called_once_with()

    def test_cancel_booking_refunds_and_records_manager(self) -> None:
        event_id = ObjectId()
        event = MagicMock(
            id=event_id,
            customer=Customer(name="Guest", email="guest@example.test", phone="+1"),
            payment=Payment(
                provider="stripe",
                reference="pi_1",
                amount=10000,
                currency="EUR",
                paid_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
            ),
        )
        event.history = []
        query = MagicMock()
        query.first.return_value = event
        cancel_query = MagicMock()
        cancel_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [query, cancel_query]
        provider = MagicMock()
        provider.refund_payment.return_value = {"status": "succeeded"}

        status, _headers, _body = admin_events.update_event(
            str(event_id),
            {"action": "cancel_booking", "comment": "Weather"},
            {"id": ObjectId(), "display_name": "Manager"},
            event_class=event_class,
            event_type_class=MagicMock(),
            payment_provider=provider,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        provider.refund_payment.assert_called_once_with("pi_1")
        cancellation = cancel_query.modify.call_args.kwargs["push__history"]
        self.assertEqual(cancellation.action, "booking_cancelled")
        self.assertEqual(cancellation.comment, "Weather")
        self.assertEqual(cancel_query.modify.call_args.kwargs["unset__customer"], 1)
        event.save.assert_not_called()

    def test_pending_refund_does_not_reopen_event(self) -> None:
        event = MagicMock(
            id=ObjectId(),
            customer=Customer(name="Guest", email="guest@example.test", phone="+1"),
            payment=Payment(
                provider="stripe",
                reference="pi_1",
                amount=10000,
                currency="EUR",
                paid_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
            ),
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = lookup
        provider = MagicMock()
        provider.refund_payment.return_value = {"status": "pending"}
        render = MagicMock(return_value="pending")

        result = admin_events.update_event(
            str(event.id),
            {"action": "cancel_booking", "comment": "Weather"},
            {"id": ObjectId(), "display_name": "Manager"},
            event_class=event_class,
            event_type_class=MagicMock(),
            payment_provider=provider,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "pending")
        self.assertEqual(render.call_args.args[1], HTTPStatus.CONFLICT)
        event_class.objects.assert_called_once_with(id=event.id)


if __name__ == "__main__":
    unittest.main()
