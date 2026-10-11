from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
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
from dropzone_ticketing.service.http import render as render_template


class EventModelTest(unittest.TestCase):
    def test_event_type_fields_store_price_in_minor_units(self) -> None:
        fields = EventType._fields
        self.assertIsInstance(fields["name"], mongoengine.StringField)
        self.assertTrue(fields["name"].unique)
        self.assertIsInstance(fields["price"], mongoengine.IntField)
        self.assertEqual(fields["price"].min_value, 0)
        self.assertEqual(fields["deposit"].min_value, 1)
        self.assertIsInstance(fields["order"], mongoengine.IntField)
        self.assertEqual(fields["order"].default, 0)
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
        self.assertEqual(fields["deposit"].min_value, 1)
        self.assertFalse(fields["completed"].default)
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
    def test_availability_window_spans_current_and_next_month_across_year(self) -> None:
        today, start, end = booking._window(
            datetime(2026, 12, 15, 12, tzinfo=timezone.utc),
            ZoneInfo("UTC"),
        )

        self.assertEqual(today.isoformat(), "2026-12-15")
        self.assertEqual(start, datetime(2026, 12, 15, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2027, 2, 1, tzinfo=timezone.utc))

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

    def test_availability_serializes_two_calendar_months(self) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event_type = SimpleNamespace(id=ObjectId(), name="Tandem", description="")
        available = SimpleNamespace(
            id=ObjectId(),
            dt=datetime(2026, 9, 30, 10, tzinfo=timezone.utc),
            duration_minutes=60,
            currency="eur",
            price=12500,
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
        availability = render.call_args.kwargs["availability"]
        self.assertEqual(len(availability), 32)
        self.assertEqual(render.call_args.kwargs["start_date"], "2026-09-30")
        self.assertEqual(render.call_args.kwargs["end_date"], "2026-10-31")
        self.assertEqual(availability["2026-09-30"]["status"], "available")
        self.assertEqual(
            availability["2026-09-30"]["events"],
            [
                {
                    "id": str(available.id),
                    "time": "10:00",
                    "duration_minutes": 60,
                    "currency": "EUR",
                    "price": "125.00",
                }
            ],
        )
        self.assertEqual(availability["2026-10-01"]["status"], "sold_out")
        self.assertEqual(availability["2026-10-02"]["status"], "unavailable")

        body = render_template(
            "booking.html",
            event_types=[event_type],
            selected_type=event_type,
            availability=availability,
            start_date="2026-09-30",
            end_date="2026-10-31",
            held_events=[],
        )[2].decode()
        self.assertIn('id="availability-calendar"', body)
        self.assertIn("numberOfMonths: 2", body)
        self.assertIn("beforeShowDay:", body)
        self.assertIn("booking-available", body)
        self.assertIn("booking-sold-out", body)
        self.assertIn("booking-unavailable", body)
        self.assertIn("day.events.forEach", body)

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
            ANY,
            reservation_token=booking._token_hash("secret"),
            reservation_expires_at__gt=datetime(2026, 9, 30, tzinfo=timezone.utc),
            customer=None,
            active=True,
        )

    def test_event_selection_renders_details_without_creating_a_hold(self) -> None:
        now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
        event = SimpleNamespace(id=ObjectId())
        query = MagicMock()
        query.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = query
        render = MagicMock(return_value="details")

        result = booking.hold(
            {"event_id": str(event.id)},
            "secret",
            event_class=event_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=now,
        )

        self.assertEqual(result, "details")
        self.assertEqual(render.call_args.args[0], "booking_checkout.html")
        self.assertFalse(render.call_args.kwargs["held"])
        query.modify.assert_not_called()

    def test_hold_conflict_does_not_override_an_existing_reservation(self) -> None:
        available_query = MagicMock()
        available_query.first.return_value = None
        own_hold_query = MagicMock()
        own_hold_query.first.return_value = None
        event_class = MagicMock()
        event_class.objects.side_effect = [available_query, own_hold_query]
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

    def test_reselecting_the_guests_hold_resumes_checkout(self) -> None:
        event_id = ObjectId()
        available_query = MagicMock()
        available_query.first.return_value = None
        own_hold_query = MagicMock()
        own_hold_query.first.return_value = SimpleNamespace(id=event_id)
        event_class = MagicMock()
        event_class.objects.side_effect = [available_query, own_hold_query]

        result = booking.hold(
            {"event_id": str(event_id)},
            "secret",
            event_class=event_class,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(
            result,
            (
                HTTPStatus.SEE_OTHER,
                [("Location", f"/book/resume?event_id={event_id}")],
                b"",
            ),
        )

    def test_resume_restores_contact_details_for_the_guest(self) -> None:
        customer = Customer(name="Guest", email="guest@example.test")
        responses = {"question": ["response"]}
        event = SimpleNamespace(
            checkout_customer=customer,
            checkout_responses=responses,
            payment_intent=None,
        )
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
        self.assertEqual(render.call_args.kwargs["responses"], responses)
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

    def test_resume_can_return_to_confirmed_contact_details(self) -> None:
        customer = Customer(name="Guest", email="guest@example.test")
        event = SimpleNamespace(checkout_customer=customer, payment_intent="pi_1")
        query = MagicMock()
        query.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = query
        provider = MagicMock()
        render = MagicMock(return_value="details")

        result = booking.resume(
            str(ObjectId()),
            "secret",
            True,
            event_class=event_class,
            render=render,
            payment_provider=provider,
            publishable_key="pk_test",
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "details")
        self.assertEqual(render.call_args.kwargs["customer"], customer)
        provider.retrieve_payment_intent.assert_not_called()

    def test_guest_can_cancel_an_active_hold_and_payment_intent(self) -> None:
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        event = SimpleNamespace(id=ObjectId(), payment_intent="pi_1")
        lookup = MagicMock()
        lookup.modify.return_value = event
        release = MagicMock()
        release.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, release]
        provider = MagicMock()
        provider.cancel_payment_intent.return_value = {"status": "canceled"}

        result = booking.cancel(
            {"event_id": str(event.id)},
            "secret",
            event_class=event_class,
            render=MagicMock(),
            payment_provider=provider,
            now=now,
        )

        self.assertEqual(result, (HTTPStatus.SEE_OTHER, [("Location", "/book")], b""))
        provider.cancel_payment_intent.assert_called_once_with("pi_1")
        self.assertEqual(release.modify.call_args.kwargs["unset__reservation_token"], 1)
        self.assertEqual(release.modify.call_args.kwargs["unset__payment_intent"], 1)

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
            deposit=2500,
            currency="EUR",
            event_type=SimpleNamespace(questions=[question]),
            reservation_nonce="hold-nonce",
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        held = SimpleNamespace(
            id=event.id,
            price=event.price,
            deposit=event.deposit,
            currency=event.currency,
            reservation_nonce="hold-nonce",
        )
        cleanup = MagicMock()
        claim_query = MagicMock()
        claim_query.modify.return_value = held
        save_query = MagicMock()
        save_query.modify.return_value = held
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, cleanup, claim_query, save_query]
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
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        customer = claim_query.modify.call_args.kwargs["set__checkout_customer"]
        self.assertIsNone(customer.phone)
        self.assertEqual(
            claim_query.modify.call_args.kwargs["set__checkout_responses"],
            {str(question.id): [first_response]},
        )
        self.assertEqual(
            claim_query.modify.call_args.kwargs["set__reservation_expires_at"],
            datetime(2026, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=15),
        )
        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        self.assertEqual(
            result[1],
            [("Location", f"/book/resume?event_id={event.id}")],
        )
        self.assertEqual(provider.create_payment_intent.call_args.kwargs["amount"], 2500)

    def test_contact_update_reuses_payment_intent(self) -> None:
        event = SimpleNamespace(
            id=ObjectId(),
            price=10000,
            deposit=2500,
            currency="EUR",
            event_type=SimpleNamespace(questions=[]),
            reservation_nonce="hold-nonce",
            payment_intent="pi_1",
            checkout_responses={"question": ["response"]},
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        save_query = MagicMock()
        save_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, save_query]
        provider = MagicMock()
        provider.update_payment_intent.return_value = {"id": "pi_1"}

        booking.contact(
            {
                "event_id": str(event.id),
                "name": "Updated",
                "email": "updated@example.test",
                "editing": "1",
            },
            "secret",
            event_class=event_class,
            render=MagicMock(),
            payment_provider=provider,
            publishable_key="pk_test",
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        provider.update_payment_intent.assert_called_once_with(
            "pi_1",
            email="updated@example.test",
        )
        provider.create_payment_intent.assert_not_called()
        self.assertEqual(
            save_query.modify.call_args.kwargs["set__checkout_responses"],
            {"question": ["response"]},
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
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "invalid")
        self.assertEqual(render.call_args.args[1], HTTPStatus.BAD_REQUEST)
        self.assertIn("valid responses", render.call_args.kwargs["error"])

    def test_missing_single_choice_response_is_rejected_before_hold(self) -> None:
        question = Question(
            text="Choose",
            responses=[QuestionResponse(label="One")],
        )
        event = SimpleNamespace(
            id=ObjectId(),
            event_type=SimpleNamespace(questions=[question]),
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = lookup
        render = MagicMock(return_value="invalid")

        result = booking.contact(
            {
                "event_id": str(event.id),
                "name": "Guest",
                "email": "guest@example.test",
            },
            "secret",
            event_class=event_class,
            render=render,
            payment_provider=MagicMock(),
            publishable_key="pk_test",
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "invalid")
        self.assertEqual(render.call_args.args[1], HTTPStatus.BAD_REQUEST)
        self.assertIn("required booking questions", render.call_args.kwargs["error"])
        lookup.modify.assert_not_called()

    def test_contact_rejects_a_fourth_concurrent_hold(self) -> None:
        event = SimpleNamespace(
            id=ObjectId(),
            event_type=SimpleNamespace(questions=[]),
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        cleanup = MagicMock()
        claim_query = MagicMock()
        claim_query.modify.side_effect = [
            mongoengine.NotUniqueError(),
            mongoengine.NotUniqueError(),
            mongoengine.NotUniqueError(),
        ]
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, cleanup, claim_query, claim_query, claim_query]
        render = MagicMock(return_value="limit")

        result = booking.contact(
            {
                "event_id": str(event.id),
                "name": "Guest",
                "email": "guest@example.test",
            },
            "secret",
            event_class=event_class,
            render=render,
            payment_provider=MagicMock(),
            publishable_key="pk_test",
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "limit")
        self.assertEqual(claim_query.modify.call_count, 3)
        self.assertIn("up to three", render.call_args.kwargs["message"])

    def test_contact_reuses_the_guests_own_hold_after_payment_failure(self) -> None:
        event_id = ObjectId()
        selected = SimpleNamespace(
            id=event_id,
            event_type=SimpleNamespace(questions=[]),
        )
        held = SimpleNamespace(
            id=event_id,
            price=10000,
            deposit=None,
            currency="EUR",
            reservation_nonce="hold-nonce",
        )
        lookup = MagicMock()
        lookup.first.return_value = selected
        cleanup = MagicMock()
        claim_query = MagicMock()
        claim_query.modify.return_value = None
        own_hold_query = MagicMock()
        own_hold_query.modify.return_value = held
        save_query = MagicMock()
        save_query.modify.return_value = held
        event_class = MagicMock()
        event_class.objects.side_effect = [
            lookup,
            cleanup,
            claim_query,
            own_hold_query,
            save_query,
        ]
        provider = MagicMock()
        provider.create_payment_intent.return_value = {"id": "pi_retry"}

        result = booking.contact(
            {
                "event_id": str(event_id),
                "name": "Guest",
                "email": "guest@example.test",
            },
            "secret",
            event_class=event_class,
            render=MagicMock(),
            payment_provider=provider,
            publishable_key="pk_test",
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        self.assertEqual(
            own_hold_query.modify.call_args.kwargs["set__checkout_customer"].email,
            "guest@example.test",
        )
        provider.create_payment_intent.assert_called_once()

    def test_checkout_requires_single_choice_but_not_multiple_answers(self) -> None:
        required_question = Question(
            text="Required",
            responses=[QuestionResponse(label="One"), QuestionResponse(label="Two")],
        )
        optional_question = Question(
            text="Optional",
            multiple=True,
            responses=[QuestionResponse(label="Any")],
        )
        event = SimpleNamespace(
            id=ObjectId(),
            event_type=SimpleNamespace(
                name="Tandem",
                questions=[required_question, optional_question],
            ),
            dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
            duration_minutes=60,
        )

        body = render_template(
            "booking_checkout.html",
            event=event,
            step="details",
            customer={},
            responses={
                str(required_question.id): [str(required_question.responses[0].id)]
            },
            editing=False,
            held=False,
            local_timezone=ZoneInfo("UTC"),
        )[2].decode()

        self.assertEqual(body.count('type="radio"'), 2)
        self.assertEqual(body.count("required>"), 2)
        self.assertEqual(body.count("checked"), 1)
        self.assertIn('type="checkbox"', body)
        self.assertNotIn("Reserved until", body)
        self.assertNotIn("Cancel hold", body)

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
        client.return_value.v1.customers.create.return_value = {"id": "cus_1"}
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
        self.assertEqual(values["customer"], "cus_1")
        self.assertEqual(values["setup_future_usage"], "off_session")
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

    @patch("dropzone_ticketing.service.payment.retrieve_payment_intent")
    @patch("dropzone_ticketing.service.payment._client")
    def test_custom_charge_reuses_original_payment_method(self, client, retrieve) -> None:
        retrieve.return_value = {"customer": "cus_1", "payment_method": "pm_1"}
        create = client.return_value.v1.payment_intents.create

        payment.charge_payment(
            "pi_1",
            amount=5000,
            currency="EUR",
            email="guest@example.test",
            event_id="event",
            idempotency_key="charge-key",
        )

        values, options = create.call_args.args
        self.assertEqual(values["payment_method"], "pm_1")
        self.assertEqual(values["customer"], "cus_1")
        self.assertTrue(values["confirm"])
        self.assertTrue(values["off_session"])
        self.assertEqual(options["idempotency_key"], "charge-key")

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
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "requires_capture")
        self.assertEqual(result["metadata"]["event_id"], "event")


class EventManagementTest(unittest.TestCase):
    def test_event_admin_forms_use_requested_control_sizes(self) -> None:
        event_type = SimpleNamespace(
            id=ObjectId(),
            name="Tandem",
            description="",
            price=20000,
            deposit=5000,
            currency="EUR",
            order=0,
            questions=[],
            active=True,
        )
        add_event = render_template(
            "admin_event_form.html",
            event_types=[event_type],
            values={},
        )[2].decode()
        event_type_list = render_template(
            "admin_event_type_list.html",
            event_types=[],
        )[2].decode()
        event_type_view = render_template(
            "admin_event_type_view.html",
            event_type=event_type,
        )[2].decode()

        self.assertIn('name="times" rows="8" cols="5"', add_event)
        self.assertIn('name="duration_minutes" type="number" min="1" size="4"', add_event)
        for body in (add_event, event_type_list, event_type_view):
            self.assertIn('name="price" type="number"', body)
            self.assertIn('step="0.01" size="8"', body)
            self.assertIn('name="deposit" type="number"', body)
            self.assertIn('maxlength="3" size="3"', body)

    def test_event_view_uses_tables_and_single_line_comment_inputs(self) -> None:
        response = QuestionResponse(label="Yes")
        question = Question(text="Ready?", responses=[response])
        event = SimpleNamespace(
            id=ObjectId(),
            dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
            event_type=SimpleNamespace(name="Tandem", questions=[question]),
            duration_minutes=60,
            price=20000,
            deposit=5000,
            currency="EUR",
            customer=SimpleNamespace(name="Guest", email="guest@example.test", phone=None),
            responses={str(question.id): [str(response.id)]},
            completed=False,
            active=True,
            history=[
                SimpleNamespace(
                    dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                    action="payment",
                    by=None,
                    comment="Deposit collected",
                    payment=SimpleNamespace(
                        reference="pi_1",
                        amount=5000,
                        currency="EUR",
                        status="succeeded",
                    ),
                )
            ],
        )

        body = render_template(
            "admin_event_view.html",
            event=event,
            held=False,
            balance=5000,
            local_timezone=ZoneInfo("UTC"),
        )[2].decode()

        self.assertIn('<th scope="row">Name</th><td>Guest</td>', body)
        self.assertIn('<th scope="row">Ready?</th>', body)
        self.assertIn('<th scope="col">Date/time</th>', body)
        self.assertIn('<th scope="col">Activity</th>', body)
        self.assertIn('<th scope="col">By</th>', body)
        self.assertIn('<th scope="col">Actions</th>', body)
        self.assertIn("payment", body)
        self.assertIn("Deposit collected", body)
        self.assertIn('name="action" value="refund"', body)
        self.assertIn("<title>Event details</title>", body)
        self.assertIn("<h1>Event details</h1>", body)
        self.assertNotIn(f"<h1>Event {event.id}</h1>", body)
        self.assertLess(body.index("<h2>History</h2>"), body.index("<h2>Add comment</h2>"))
        self.assertIn("<h2>Remove event</h2>", body)
        self.assertIn('name="action" value="remove"', body)
        self.assertIn("<button type=\"submit\">Proceed</button>", body)
        self.assertIn('name="price" type="number" min="0" step="0.01" size="8"', body)
        self.assertIn('name="deposit" type="number" min="0.01" step="0.01" size="8"', body)
        self.assertIn('name="currency" minlength="3" maxlength="3" size="3"', body)
        self.assertNotIn("<dl", body)
        self.assertNotIn("<ol", body)
        self.assertNotIn("<textarea", body)

    def test_event_update_keeps_schedule_fields_immutable(self) -> None:
        event = SimpleNamespace(id=ObjectId())
        lookup = MagicMock()
        lookup.first.return_value = event
        update_query = MagicMock()
        update_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, update_query]
        event_type_class = MagicMock()

        result = admin_events.update_event(
            str(event.id),
            {
                "action": "update",
                "dt": "2030-01-01T12:00",
                "event_type": str(ObjectId()),
                "duration_minutes": "120",
                "price": "249.50",
                "currency": "usd",
            },
            {"id": ObjectId(), "display_name": "Manager"},
            event_class=event_class,
            event_type_class=event_type_class,
            payment_provider=MagicMock(),
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        updates = update_query.modify.call_args.kwargs
        self.assertNotIn("set__dt", updates)
        self.assertNotIn("set__event_type", updates)
        self.assertNotIn("set__duration_minutes", updates)
        self.assertEqual(updates["set__price"], 24950)
        self.assertEqual(updates["set__currency"], "USD")
        event_type_class.objects.assert_not_called()

    def test_event_type_and_question_orders_can_be_updated(self) -> None:
        question = Question(text="Experience?", order=1)
        event_type = MagicMock(order=0, questions=[question])
        lookup = MagicMock()
        lookup.first.return_value = event_type
        event_type_class = MagicMock()
        event_type_class.objects.return_value = lookup

        admin_events.update_event_type(
            str(ObjectId()),
            {
                "action": "update",
                "name": "Tandem",
                "price": "199.00",
                "currency": "eur",
                "order": "20",
            },
            event_type_class=event_type_class,
            render=MagicMock(),
        )
        self.assertEqual(event_type.order, 20)

        admin_events.update_event_type(
            str(ObjectId()),
            {
                "action": "update_question_order",
                "question_id": str(question.id),
                "order": "30",
            },
            event_type_class=event_type_class,
            render=MagicMock(),
        )
        self.assertEqual(question.order, 30)

    def test_admin_can_cancel_an_active_hold(self) -> None:
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        event = MagicMock(
            id=ObjectId(),
            customer=None,
            payment_intent="pi_1",
        )
        event.is_reserved.return_value = True
        lookup = MagicMock()
        lookup.first.return_value = event
        claim_query = MagicMock()
        claim_query.modify.return_value = event
        cancel_query = MagicMock()
        cancel_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, claim_query, cancel_query]
        provider = MagicMock()
        provider.cancel_payment_intent.return_value = {"status": "canceled"}

        result = admin_events.update_event(
            str(event.id),
            {"action": "cancel_hold", "comment": "Released"},
            {"id": ObjectId(), "display_name": "Manager"},
            event_class=event_class,
            event_type_class=MagicMock(),
            payment_provider=provider,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=now,
        )

        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        provider.cancel_payment_intent.assert_called_once_with("pi_1")
        self.assertEqual(cancel_query.modify.call_args.kwargs["unset__reservation_token"], 1)

    def test_admin_completion_charges_price_minus_deposit(self) -> None:
        event = SimpleNamespace(
            id=ObjectId(),
            customer=Customer(name="Guest", email="guest@example.test"),
            price=10000,
            currency="EUR",
            completed=False,
            history=[
                EventHistoryItem(
                    dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                    action="payment",
                    payment=Payment(
                        provider="stripe",
                        reference="pi_deposit",
                        amount=2500,
                        currency="EUR",
                        dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                    ),
                )
            ],
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        claim_query = MagicMock()
        claim_query.modify.return_value = event
        update_query = MagicMock()
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, claim_query, update_query]
        provider = MagicMock()
        provider.charge_payment.return_value = {
            "id": "pi_balance",
            "status": "succeeded",
            "amount_received": 7500,
            "currency": "eur",
        }

        result = admin_events.update_event(
            str(event.id),
            {"action": "complete"},
            {"id": ObjectId(), "display_name": "Manager"},
            event_class=event_class,
            event_type_class=MagicMock(),
            payment_provider=provider,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        self.assertEqual(provider.charge_payment.call_args.kwargs["amount"], 7500)
        self.assertTrue(update_query.modify.call_args.kwargs["set__completed"])
        history = update_query.modify.call_args.kwargs["push_all__history"]
        self.assertEqual([item.action for item in history], ["payment", "comment"])

    def test_admin_can_refund_a_custom_amount(self) -> None:
        payment_record = Payment(
            provider="stripe",
            reference="pi_1",
            amount=10000,
            currency="EUR",
            dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
        event = SimpleNamespace(
            id=ObjectId(),
            history=[
                EventHistoryItem(
                    dt=datetime(2026, 9, 30, tzinfo=timezone.utc),
                    action="payment",
                    payment=payment_record,
                )
            ],
        )
        lookup = MagicMock()
        lookup.first.return_value = event
        claim_query = MagicMock()
        claim_query.modify.return_value = event
        update_query = MagicMock()
        update_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, claim_query, update_query]
        provider = MagicMock()
        provider.refund_payment.return_value = {
            "id": "re_1",
            "status": "succeeded",
            "currency": "eur",
        }

        result = admin_events.update_event(
            str(event.id),
            {"action": "refund", "reference": "pi_1", "amount": "25.00"},
            {"id": ObjectId(), "display_name": "Manager"},
            event_class=event_class,
            event_type_class=MagicMock(),
            payment_provider=provider,
            render=MagicMock(),
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(result[0], HTTPStatus.SEE_OTHER)
        self.assertEqual(provider.refund_payment.call_args.kwargs["amount"], 2500)
        refund = update_query.modify.call_args.kwargs["push__history"].payment
        self.assertEqual(refund.amount, 2500)
        self.assertEqual(refund.source_reference, "pi_1")

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

    def test_create_events_uses_times_and_records_user_history(self) -> None:
        event_type = SimpleNamespace(id=ObjectId())
        type_query = MagicMock()
        type_query.first.return_value = event_type
        event_type_class = MagicMock()
        event_type_class.objects.return_value = type_query
        events = [MagicMock(id=ObjectId()), MagicMock(id=ObjectId())]
        event_class = MagicMock(side_effect=events)
        user_id = ObjectId()

        status, headers, _body = admin_events.create_event(
            {
                "date": "2026-10-01",
                "times": "10:30\n12:00",
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
        self.assertEqual(headers, [("Location", "/admin/event/list")])
        self.assertEqual(event_class.call_count, 2)
        first_values = event_class.call_args_list[0].kwargs
        second_values = event_class.call_args_list[1].kwargs
        self.assertEqual(first_values["dt"], datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(second_values["dt"], datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(first_values["duration_minutes"], 90)
        self.assertEqual(first_values["price"], 19995)
        self.assertEqual(first_values["currency"], "EUR")
        history = first_values["history"][0]
        self.assertEqual(history.action, "comment")
        self.assertEqual(history.by.id, user_id)
        self.assertEqual(history.by.display_name, "Manager")
        self.assertNotIn("questions", first_values)
        for event in events:
            event.save.assert_called_once_with()

    def test_create_events_rejects_duplicate_times(self) -> None:
        event_type = SimpleNamespace(id=ObjectId())
        event_type_class = MagicMock()
        event_type_class.objects.return_value.first.return_value = event_type
        render = MagicMock(return_value="invalid")

        result = admin_events.create_event(
            {
                "date": "2026-10-01",
                "times": "10:30\n10:30",
                "duration_minutes": "90",
                "event_type": str(event_type.id),
                "price": "199.95",
                "currency": "eur",
            },
            {"id": ObjectId(), "display_name": "Manager"},
            event_class=MagicMock(),
            event_type_class=event_type_class,
            render=render,
            local_timezone=ZoneInfo("UTC"),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result, "invalid")
        self.assertEqual(render.call_args.args[1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(render.call_args.kwargs["error"], "Enter each event time only once.")

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
        claim_query = MagicMock()
        claim_query.modify.return_value = event
        refund_query = MagicMock()
        refund_query.modify.return_value = event
        cancel_query = MagicMock()
        cancel_query.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.side_effect = [query, claim_query, refund_query, cancel_query]
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
        provider.refund_payment.assert_called_once_with(
            "pi_1",
            amount=10000,
            idempotency_key=f"event-{event_id}-cancel-pi_1-10000-1",
        )
        refund_history = refund_query.modify.call_args.kwargs["push__history"]
        cancellation = cancel_query.modify.call_args.kwargs["push__history"]
        self.assertEqual(cancellation.comment, "Weather")
        self.assertEqual(refund_history.payment.reference, "re_1")
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
        claim_query = MagicMock()
        claim_query.modify.return_value = event
        record_query = MagicMock()
        record_query.modify.return_value = event
        release_query = MagicMock()
        event_class = MagicMock()
        event_class.objects.side_effect = [lookup, claim_query, record_query, release_query]
        provider = MagicMock()
        provider.refund_payment.return_value = {
            "id": "re_1",
            "status": "pending",
            "currency": "eur",
        }
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
        release_query.update.assert_called_once_with(
            unset__payment_operation=1,
            unset__payment_operation_expires_at=1,
        )


if __name__ == "__main__":
    unittest.main()
