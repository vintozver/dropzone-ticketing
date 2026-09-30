from __future__ import annotations

import io
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import MagicMock

import mongoengine
from bson import ObjectId

from dropzone_ticketing.model import mongoengine_alias
from dropzone_ticketing.model.event import Event, EventBooking
from dropzone_ticketing.model.ticket import UserRef
from dropzone_ticketing.service.actions.events import create_event, update_booking
from dropzone_ticketing.service.routes import dispatch


class EventModelTest(unittest.TestCase):
    def test_event_schema_supports_capacity_and_bookings(self) -> None:
        fields = Event._fields

        self.assertIsInstance(fields["name"], mongoengine.StringField)
        self.assertTrue(fields["name"].required)
        self.assertIsInstance(fields["description"], mongoengine.StringField)
        self.assertFalse(fields["description"].required)
        self.assertIsInstance(fields["starts"], mongoengine.DateTimeField)
        self.assertTrue(fields["starts"].required)
        self.assertIsInstance(fields["capacity"], mongoengine.IntField)
        self.assertEqual(fields["capacity"].min_value, 1)
        self.assertIsInstance(fields["created_by"], mongoengine.EmbeddedDocumentField)
        self.assertIs(fields["created_by"].document_type_obj, UserRef)
        self.assertIsInstance(fields["bookings"], mongoengine.EmbeddedDocumentListField)
        self.assertIs(fields["bookings"].field.document_type_obj, EventBooking)
        self.assertIs(Event._meta["db_alias"], mongoengine_alias)

    def test_booking_records_user_and_time(self) -> None:
        fields = EventBooking._fields

        self.assertIs(fields["user"].document_type_obj, UserRef)
        self.assertTrue(fields["user"].required)
        self.assertTrue(fields["dt"].required)


class EventActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.viewer = {
            "id": ObjectId("507f1f77bcf86cd799439011"),
            "display_name": "Jane",
            "roles": ["solo"],
        }

    def test_create_event_persists_valid_form(self) -> None:
        event = MagicMock(id=ObjectId("507f1f77bcf86cd799439012"))
        event_class = MagicMock(return_value=event)

        status, headers, body = create_event(
            {
                "name": "Safety day",
                "starts": "2026-10-02T12:30",
                "capacity": "20",
                "description": "Annual briefing",
            },
            self.viewer,
            event_class=event_class,
            user_ref_class=UserRef,
            render=MagicMock(),
            local_timezone=timezone.utc,
            now=self.now,
        )

        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertEqual(headers, [("Location", f"/event/{event.id}")])
        self.assertEqual(body, b"")
        event.save.assert_called_once_with()
        values = event_class.call_args.kwargs
        self.assertEqual(values["name"], "Safety day")
        self.assertEqual(values["capacity"], 20)
        self.assertEqual(values["starts"], datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc))
        self.assertEqual(values["created_by"].id, self.viewer["id"])

    def test_create_event_rejects_past_start(self) -> None:
        render = MagicMock(return_value=("rendered", [], b""))

        create_event(
            {"name": "Old event", "starts": "2026-09-30T12:30", "capacity": "20"},
            self.viewer,
            event_class=MagicMock(),
            user_ref_class=UserRef,
            render=render,
            local_timezone=timezone.utc,
            now=self.now,
        )

        self.assertEqual(render.call_args.args[:2], ("event_create.html", HTTPStatus.BAD_REQUEST))
        self.assertEqual(render.call_args.kwargs["error"], "Start time must be in the future.")

    def test_booking_uses_capacity_compare_and_swap(self) -> None:
        event = SimpleNamespace(
            id=ObjectId("507f1f77bcf86cd799439012"),
            name="Safety day",
            description="",
            starts=self.now + timedelta(days=1),
            capacity=1,
            bookings=[],
        )
        queryset = MagicMock()
        queryset.first.return_value = event
        queryset.modify.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = queryset

        status, headers, _body = update_booking(
            str(event.id),
            "book",
            self.viewer,
            event_class=event_class,
            booking_class=EventBooking,
            user_ref_class=UserRef,
            render=MagicMock(),
            now=self.now,
        )

        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertEqual(headers, [("Location", f"/event/{event.id}")])
        modify_args = queryset.modify.call_args.kwargs
        self.assertTrue(modify_args["new"])
        self.assertEqual(modify_args["push__bookings"].user.id, self.viewer["id"])

    def test_booking_rejects_a_full_event(self) -> None:
        event = SimpleNamespace(
            id=ObjectId("507f1f77bcf86cd799439012"),
            name="Safety day",
            description="",
            starts=self.now + timedelta(days=1),
            capacity=1,
            bookings=[
                EventBooking(
                    user=UserRef(id=ObjectId("507f1f77bcf86cd799439013"), display_name="Sam"),
                    dt=self.now,
                )
            ],
        )
        queryset = MagicMock()
        queryset.first.return_value = event
        event_class = MagicMock()
        event_class.objects.return_value = queryset
        render = MagicMock(return_value=("rendered", [], b""))

        update_booking(
            str(event.id),
            "book",
            self.viewer,
            event_class=event_class,
            booking_class=EventBooking,
            user_ref_class=UserRef,
            render=render,
            now=self.now,
        )

        self.assertEqual(render.call_args.args[:2], ("event.html", HTTPStatus.CONFLICT))
        self.assertEqual(render.call_args.kwargs["error"], "This event is fully booked.")
        queryset.modify.assert_not_called()


class EventRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.viewer = {
            "id": ObjectId("507f1f77bcf86cd799439011"),
            "display_name": "Jane",
            "roles": ["solo"],
        }
        self.handlers = MagicMock()
        self.handlers._require_auth.return_value = None
        self.handlers._require_admin.return_value = None
        self.handlers._current_user_ref.return_value = self.viewer
        self.handlers._read_form.return_value = {"action": "book"}
        self.handlers._list_events.return_value = (HTTPStatus.OK, [], b"events")
        self.handlers._view_event.return_value = (HTTPStatus.OK, [], b"event")
        self.handlers._update_booking.return_value = (HTTPStatus.SEE_OTHER, [], b"")

    def request(self, path: str, method: str = "GET", body: bytes = b""):
        return dispatch(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": method,
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
            },
            self.handlers,
        )

    def test_event_list_requires_authentication_and_uses_viewer(self) -> None:
        status, _headers, body = self.request("/events")

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body, b"events")
        self.handlers._require_auth.assert_called_once()
        self.handlers._list_events.assert_called_once_with(self.viewer)

    def test_event_route_books_for_current_user(self) -> None:
        event_id = "507f1f77bcf86cd799439012"
        status, _headers, _body = self.request(
            f"/event/{event_id}",
            "POST",
            b"action=book",
        )

        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.handlers._update_booking.assert_called_once_with(event_id, "book", self.viewer)

    def test_event_creation_requires_admin(self) -> None:
        self.handlers._require_admin.return_value = (HTTPStatus.FORBIDDEN, [], b"denied")

        status, _headers, body = self.request("/admin/event/new")

        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body, b"denied")
        self.handlers._require_admin.assert_called_once()


if __name__ == "__main__":
    unittest.main()
