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
    EventHistoryItem,
    EventType,
    Payment,
    Question,
    QuestionResponse,
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
        self.assertNotIn("payment", fields)
        self.assertIs(fields["history"].field.document_type_obj, EventHistoryItem)
        self.assertNotIn("questions", fields)
        self.assertIsInstance(fields["responses"], mongoengine.DictField)
        self.assertIsInstance(fields["price"], mongoengine.IntField)
        self.assertIsInstance(fields["currency"], mongoengine.StringField)
        self.assertIsInstance(fields["reservation_expires_at"], mongoengine.DateTimeField)
        self.assertIsInstance(fields["reservation_slot"], mongoengine.IntField)
        self.assertEqual(fields["reservation_slot"].max_value, 2)
        self.assertIs(Event._meta["db_alias"], mongoengine_alias)

    def test_customer_phone_is_optional_and_payment_uses_dt(self) -> None:
        self.assertFalse(Customer._fields["phone"].required)
        self.assertIn("dt", Payment._fields)
        self.assertNotIn("paid_at", Payment._fields)

    def test_history_items_have_constrained_actions_and_optional_actor(self) -> None:
        fields = EventHistoryItem._fields
        self.assertFalse(fields["by"].required)
        self.assertEqual(
            fields["action"].choices,
            ("payment", "refund", "comment", "book", "cancel"),
        )
        self.assertIs(fields["payment"].document_type_obj, Payment)

    def test_questions_and_responses_have_unique_default_identifiers(self) -> None:
        first_question = Question(
            text="Experience?",
            responses=[QuestionResponse(label="None")],
        )
        second_question = Question(
            text="Weight?",
            responses=[QuestionResponse(label="Under 90 kg")],
        )
        self.assertIsInstance(Question._fields["id"], mongoengine.ObjectIdField)
        self.assertIsInstance(QuestionResponse._fields["id"], mongoengine.ObjectIdField)
        self.assertIsInstance(first_question.id, ObjectId)
        self.assertIsInstance(first_question.responses[0].id, ObjectId)
        self.assertNotEqual(first_question.id, second_question.id)
        self.assertNotEqual(
            first_question.responses[0].id,
            second_question.responses[0].id,
        )

    def test_duplicate_question_and_response_identifiers_are_rejected(self) -> None:
        first = Question(
            text="First",
            responses=[QuestionResponse(label="One")],
        )
        duplicate_question = Question(
            id=first.id,
            text="Second",
            responses=[QuestionResponse(label="Two")],
        )
        with self.assertRaisesRegex(mongoengine.ValidationError, "Question identifiers"):
            EventType(
                name="Tandem",
                price=10000,
                currency="EUR",
                questions=[first, duplicate_question],
            ).validate()

        duplicate_response = Question(
            text="Second",
            responses=[
                QuestionResponse(id=first.responses[0].id, label="Duplicate")
            ],
        )
        with self.assertRaisesRegex(mongoengine.ValidationError, "Response identifiers"):
            EventType(
                name="AFF",
                price=10000,
                currency="EUR",
                questions=[first, duplicate_response],
            ).validate()

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

class BookingActionTest(unittest.TestCase):
    def test_invalid_event_type_is_reported_as_bad_request(self) -> None:
        event_types = MagicMock()
        event_types.order_by.return_value = []
        event_type_class = MagicMock()
        event_type_class.objects.return_value = event_types
        render = MagicMock(return_value="invalid")

        result = booking.availability(
            "not-an-object-id",
            None,
            event_class=MagicMock(),
            event_type_class=event_type_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "invalid")
        self.assertEqual(render.call_args.args[1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(render.call_args.kwargs["message"], "Choose a valid event type.")

    def test_invalid_event_id_is_reported_as_bad_request(self) -> None:
        render = MagicMock(return_value="invalid")

        result = booking.hold(
            {"event_id": "not-an-object-id"},
            "secret",
            event_class=MagicMock(),
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "invalid")
        self.assertEqual(render.call_args.args[1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(render.call_args.kwargs["message"], "Choose a valid event.")

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
            None,
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

    def test_availability_lists_the_guests_active_holds(self) -> None:
        held = SimpleNamespace(dt=datetime(2026, 10, 1, tzinfo=timezone.utc))
        event_type_query = MagicMock()
        event_type_query.order_by.return_value = []
        held_query = MagicMock()
        held_query.order_by.return_value = [held]
        event_class = MagicMock()
        event_class.objects.return_value = held_query
        event_type_class = MagicMock()
        event_type_class.objects.return_value = event_type_query
        render = MagicMock(return_value="rendered")

        booking.availability(
            None,
            "secret",
            event_class=event_class,
            event_type_class=event_type_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(render.call_args.kwargs["held_events"], [held])
        event_class.objects.assert_called_once_with(
            reservation_token=booking._token_hash("secret"),
            reservation_expires_at__gt=datetime(2026, 9, 30, tzinfo=timezone.utc),
            customer=None,
            active=True,
        )

    def test_hold_is_atomic_and_last_exactly_fifteen_minutes(self) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event = SimpleNamespace(id=ObjectId())
        query = MagicMock()
        query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = query

        result = booking.hold(
            {"event_id": str(event.id)},
            "secret",
            event_class=event_class,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=now,
        )

        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        self.assertEqual(result[1], [("Location", f"/book/resume?event_id={event.id}")])
        self.assertEqual(
            query.modify.call_args.kwargs["set__reservation_expires_at"],
            now + timedelta(minutes=15),
        )
        self.assertEqual(
            query.modify.call_args.kwargs["set__reservation_token"],
            booking._token_hash("secret"),
        )
        self.assertEqual(query.modify.call_args.kwargs["set__reservation_slot"], 0)

    def test_hold_conflict_does_not_override_an_existing_reservation(self) -> None:
        query = MagicMock()
        query.modify.return_value = None
        query.first.return_value = None
        event_class = MagicMock()
        event_class.objects.return_value = query
        render = MagicMock(return_value="conflict")

        result = booking.hold(
            {"event_id": str(ObjectId())},
            "secret",
            event_class=event_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "conflict")
        self.assertEqual(render.call_args.args[1], HTTPStatus.CONFLICT)

    def test_repeated_hold_resumes_the_guests_existing_reservation(self) -> None:
        event_id = ObjectId()
        query = MagicMock()
        query.modify.return_value = None
        query.first.return_value = SimpleNamespace(id=event_id)
        event_class = MagicMock()
        event_class.objects.return_value = query

        result = booking.hold(
            {"event_id": str(event_id)},
            "secret",
            event_class=event_class,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        self.assertEqual(result[1], [("Location", f"/book/resume?event_id={event_id}")])

    def test_hold_rejects_a_fourth_concurrent_slot(self) -> None:
        query = MagicMock()
        query.modify.side_effect = [
            mongoengine.NotUniqueError(),
            mongoengine.NotUniqueError(),
            mongoengine.NotUniqueError(),
        ]
        event_class = MagicMock()
        event_class.objects.return_value = query
        render = MagicMock(return_value="limit")

        result = booking.hold(
            {"event_id": str(ObjectId())},
            "secret",
            event_class=event_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "limit")
        self.assertEqual(query.modify.call_count, 3)
        self.assertIn("up to three", render.call_args.kwargs["message"])

    def test_resume_restores_contact_details_for_the_guest(self) -> None:
        customer = Customer(name="Guest", email="guest@example.test")
        event = SimpleNamespace(checkout_customer=customer, payment_intent=None)
        query = MagicMock()
        query.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = query
        render = MagicMock(return_value="details")

        result = booking.resume(
            str(ObjectId()),
            "secret",
            event_class=event_class,
            render=render,
            payment_provider=MagicMock(),
            publishable_key="pk_test",
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "details")
        self.assertEqual(render.call_args.kwargs["customer"], customer)
        self.assertEqual(render.call_args.kwargs["step"], "details")

    def test_resume_restores_authorized_payment(self) -> None:
        event = SimpleNamespace(
            checkout_customer=Customer(name="Guest", email="guest@example.test"),
            payment_intent="pi_1",
        )
        query = MagicMock()
        query.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = query
        provider = MagicMock()
        provider.retrieve_payment_intent.return_value = {
            "status": "requires_capture",
            "client_secret": "pi_secret",
        }
        render = MagicMock(return_value="payment")

        result = booking.resume(
            str(ObjectId()),
            "secret",
            event_class=event_class,
            render=render,
            payment_provider=provider,
            publishable_key="pk_test",
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "payment")
        self.assertTrue(render.call_args.kwargs["payment_ready"])
        self.assertEqual(render.call_args.kwargs["payment_intent"], "pi_1")

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
            checkout_responses={"question": ["response"]},
            reservation_nonce="hold-nonce",
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
        provider = MagicMock()
        provider.retrieve_payment_intent.return_value = {
            "id": "pi_1",
            "status": "requires_capture",
            "amount": 12500,
            "currency": "eur",
            "metadata": {"event_id": str(event_id), "reservation_token": "hold-nonce"},
        }
        provider.capture_payment_intent.return_value = {
            "status": "succeeded",
            "amount_received": 12500,
            "currency": "eur",
        }
        render = MagicMock(return_value="confirmed")

        result = booking.complete(
            {"event_id": str(event_id), "payment_intent": "pi_1"},
            "secret",
            event_class=event_class,
            render=render,
            payment_provider=provider,
            now=now,
        )

        self.assertEqual(result, "confirmed")
        provider.capture_payment_intent.assert_called_once_with("pi_1")
        self.assertEqual(authorize_query.modify.call_args.kwargs["set__customer"], customer)
        history = finish_query.modify.call_args.kwargs["push_all__history"]
        self.assertEqual([item.action for item in history], ["payment", "book"])
        recorded_payment = history[0].payment
        self.assertEqual(recorded_payment.reference, "pi_1")
        self.assertEqual(recorded_payment.amount, 12500)
        self.assertEqual(
            finish_query.modify.call_args.kwargs["set__responses"],
            {"question": ["response"]},
        )

    def test_contact_accepts_optional_phone_and_records_question_responses(self) -> None:
        question = Question(
            text="Choose",
            multiple=True,
            responses=[
                QuestionResponse(label="One"),
                QuestionResponse(label="Two"),
            ],
        )
        event = SimpleNamespace(
            id=ObjectId(),
            price=10000,
            currency="EUR",
            event_type=SimpleNamespace(questions=[question]),
            reservation_nonce="hold-nonce",
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        updated = MagicMock(id=event.id)
        save_query = MagicMock()
        save_query.modify.return_value = updated
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, save_query]
        provider = MagicMock()
        provider.create_payment_intent.return_value = {
            "id": "pi_1",
            "client_secret": "secret",
        }
        first_response = str(question.responses[0].id)

        result = booking.contact(
            {
                "event_id": str(event.id),
                "name": "Guest",
                "email": "guest@example.test",
                f"question_{question.id}_{first_response}": "on",
            },
            "secret",
            event_class=event_class,
            render=MagicMock(return_value="payment"),
            payment_provider=provider,
            publishable_key="pk_test",
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        customer = save_query.modify.call_args.kwargs["set__checkout_customer"]
        self.assertIsNone(customer.phone)
        self.assertEqual(
            save_query.modify.call_args.kwargs["set__checkout_responses"],
            {str(question.id): [first_response]},
        )
        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        self.assertEqual(
            result[1],
            [("Location", f"/book/resume?event_id={event.id}")],
        )

    def test_invalid_single_choice_response_is_rejected(self) -> None:
        question = Question(
            text="Choose",
            responses=[QuestionResponse(label="One")],
        )
        event = SimpleNamespace(
            id=ObjectId(),
            event_type=SimpleNamespace(questions=[question]),
        )
        query = MagicMock()
        query.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = query
        render = MagicMock(return_value="invalid")

        result = booking.contact(
            {
                "event_id": str(event.id),
                "name": "Guest",
                "email": "guest@example.test",
                f"question_{question.id}": str(ObjectId()),
            },
            "secret",
            event_class=event_class,
            render=render,
            payment_provider=MagicMock(),
            publishable_key="pk_test",
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "invalid")
        self.assertEqual(render.call_args.args[1], HTTPStatus.BAD_REQUEST)
        self.assertIn("valid responses", render.call_args.kwargs["error"])

    def test_capture_provider_error_is_reported_and_releases_claim(self) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event_id = ObjectId()
        event = SimpleNamespace(
            id=event_id,
            event_type=SimpleNamespace(),
            price=12500,
            currency="EUR",
            checkout_customer=Customer(
                name="Guest",
                email="guest@example.test",
                phone="+1",
            ),
            checkout_responses={},
            reservation_nonce="hold-nonce",
        )
        lookup_query = MagicMock()
        lookup_query.first.return_value = event
        authorize_query = MagicMock()
        authorize_query.modify.return_value = event
        release_query = MagicMock()
        event_class = MagicMock()
        event_class.objects.side_effect = [
            lookup_query,
            authorize_query,
            release_query,
        ]
        provider = MagicMock()
        provider.retrieve_payment_intent.return_value = {
            "status": "requires_capture",
            "amount": 12500,
            "currency": "eur",
            "metadata": {
                "event_id": str(event_id),
                "reservation_token": "hold-nonce",
            },
        }
        provider.capture_payment_intent.side_effect = ValueError("provider unavailable")
        render = MagicMock(return_value="capture-error")

        result = booking.complete(
            {"event_id": str(event_id), "payment_intent": "pi_1"},
            "secret",
            event_class=event_class,
            render=render,
            payment_provider=provider,
            now=now,
        )

        self.assertEqual(result, "capture-error")
        self.assertEqual(render.call_args.args[1], HTTPStatus.BAD_GATEWAY)
        release_query.modify.assert_called_once_with(unset__customer=1)

    def test_unexpected_capture_error_is_not_caught(self) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event_id = ObjectId()
        event = SimpleNamespace(
            id=event_id,
            event_type=SimpleNamespace(),
            price=12500,
            currency="EUR",
            checkout_customer=Customer(
                name="Guest",
                email="guest@example.test",
                phone="+1",
            ),
            checkout_responses={},
            reservation_nonce="hold-nonce",
        )
        lookup_query = MagicMock()
        lookup_query.first.return_value = event
        authorize_query = MagicMock()
        authorize_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup_query, authorize_query]
        provider = MagicMock()
        provider.retrieve_payment_intent.return_value = {
            "status": "requires_capture",
            "amount": 12500,
            "currency": "eur",
            "metadata": {
                "event_id": str(event_id),
                "reservation_token": "hold-nonce",
            },
        }
        provider.capture_payment_intent.side_effect = RuntimeError("programming error")

        with self.assertRaisesRegex(RuntimeError, "programming error"):
            booking.complete(
                {"event_id": str(event_id), "payment_intent": "pi_1"},
                "secret",
                event_class=event_class,
                render=MagicMock(),
                payment_provider=provider,
                now=now,
            )


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
        self.assertNotIn("payment_method_types", values)
        self.assertEqual(
            create.call_args.args[1]["idempotency_key"],
            "reservation-hash-event",
        )

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
    def test_event_type_question_management_creates_and_removes_unique_ids(self) -> None:
        event_type = MagicMock()
        event_type.questions = []
        lookup = MagicMock()
        lookup.first.return_value = event_type
        event_type_class = MagicMock()
        event_type_class.objects.return_value = lookup

        admin_events.update_event_type(
            str(ObjectId()),
            {
                "action": "add_question",
                "question": "Experience?",
                "order": "10",
                "multiple": "on",
                "visible": "on",
                "responses": "None\nSome",
            },
            event_type_class=event_type_class,
            render=MagicMock(),
        )

        question = event_type.questions[0]
        self.assertEqual(question.order, 10)
        self.assertTrue(question.multiple)
        self.assertTrue(question.visible)
        self.assertEqual([response.label for response in question.responses], ["None", "Some"])
        self.assertNotEqual(question.responses[0].id, question.responses[1].id)

        admin_events.update_event_type(
            str(ObjectId()),
            {
                "action": "remove_question",
                "question_id": str(question.id),
            },
            event_type_class=event_type_class,
            render=MagicMock(),
        )
        self.assertEqual(event_type.questions, [])

    def test_create_event_uses_explicit_price_and_records_user_history(self) -> None:
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
        self.assertEqual(history.action, "comment")
        self.assertEqual(history.by.id, user_id)
        self.assertEqual(history.by.display_name, "Manager")
        self.assertNotIn("questions", event_class.call_args.kwargs)
        event.save.assert_called_once_with()

    def test_cancel_booking_refunds_and_records_manager(self) -> None:
        event_id = ObjectId()
        event = MagicMock(
            id=event_id,
            customer=Customer(name="Guest", email="guest@example.test", phone="+1"),
        )
        event.history = [
            EventHistoryItem(
                dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                action="payment",
                payment=Payment(
                    provider="stripe",
                    reference="pi_1",
                    amount=10000,
                    currency="EUR",
                    dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                ),
            )
        ]
        query = MagicMock()
        query.first.return_value = event
        cancel_query = MagicMock()
        cancel_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [query, cancel_query]
        provider = MagicMock()
        provider.refund_payment.return_value = {
            "id": "re_1",
            "status": "succeeded",
            "amount": 10000,
            "currency": "eur",
        }

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
        history = cancel_query.modify.call_args.kwargs["push_all__history"]
        self.assertEqual([item.action for item in history], ["refund", "cancel"])
        cancellation = history[1]
        self.assertEqual(cancellation.comment, "Weather")
        self.assertEqual(history[0].payment.reference, "re_1")
        self.assertEqual(cancel_query.modify.call_args.kwargs["unset__customer"], 1)
        self.assertEqual(cancel_query.modify.call_args.kwargs["unset__responses"], 1)
        event.save.assert_not_called()

    def test_pending_refund_does_not_reopen_event(self) -> None:
        event = MagicMock(
            id=ObjectId(),
            customer=Customer(name="Guest", email="guest@example.test", phone="+1"),
        )
        event.history = [
            EventHistoryItem(
                dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                action="payment",
                payment=Payment(
                    provider="stripe",
                    reference="pi_1",
                    amount=10000,
                    currency="EUR",
                    dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                ),
            )
        ]
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
