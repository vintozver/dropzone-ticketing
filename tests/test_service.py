from __future__ import annotations

import io
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import ANY, MagicMock, call, patch
from urllib.parse import urlencode

from bson import ObjectId
from google.auth.exceptions import GoogleAuthError
from mongoengine.errors import NotUniqueError

from dropzone_ticketing import service
from dropzone_ticketing.model.ticket import Redemption, UserRef
from dropzone_ticketing.service import config
from dropzone_ticketing.service import google as google_module
from dropzone_ticketing.service import register as register_module
from dropzone_ticketing.service.actions.search_users import search_users
from dropzone_ticketing.service.actions.view_issued_tickets import view_issued_tickets
from dropzone_ticketing.service.actions.view_redeemed_tickets import view_redeemed_tickets


def b64(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def user_ref(display_name: str, *, object_id: str | None = None) -> UserRef:
    return UserRef(id=ObjectId(object_id) if object_id else None, display_name=display_name)


class ServiceHelperTest(unittest.TestCase):
    def test_generated_code_is_printable_ascii_with_exact_length(self) -> None:
        for _ in range(100):
            code = service.generate_code()
            self.assertEqual(len(code), 16)
            self.assertTrue(all(33 <= ord(character) <= 126 for character in code))

    def test_codes_are_split_on_all_whitespace(self) -> None:
        self.assertEqual(
            service.split_codes("one two\tthree\nfour\r\nfive"),
            ["one", "two", "three", "four", "five"],
        )

    def test_pdf_filename_is_safe_for_response_headers(self) -> None:
        self.assertEqual(service._safe_filename("Jane\r\nJumper / ✈"), "tickets-Jane-Jumper.pdf")

    @patch.object(service, "generate_code", side_effect=["duplicate-code", "new-code"])
    @patch.object(service, "Ticket")
    def test_issue_retries_a_code_collision(self, ticket_class, generate_code) -> None:
        duplicate = MagicMock()
        duplicate.save.side_effect = NotUniqueError
        saved = MagicMock(code="new-code", id="507f1f77bcf86cd799439011")
        ticket_class.side_effect = [duplicate, saved]

        status, _headers, body = service._issue(
            {
                "to_display_name": "Jane",
                "count": "1",
                "payment": "cash",
                "purpose": "C182 hop-and-hop",
            },
            {"id": ObjectId("507f1f77bcf86cd7994390aa"), "display_name": "Issuer"},
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertNotIn(b"new-code", body)
        self.assertEqual(generate_code.call_count, 2)
        self.assertEqual(ticket_class.call_count, 2)
        for call_ in ticket_class.call_args_list:
            self.assertEqual(call_.kwargs["payment"], "cash")
            self.assertEqual(call_.kwargs["purpose"], "C182 hop-and-hop")
            self.assertEqual(call_.kwargs["issued_to"].display_name, "Jane")
            self.assertEqual(call_.kwargs["issued_by"].display_name, "Issuer")

    @patch.object(service, "Ticket")
    def test_redeem_reports_each_result(self, ticket_class) -> None:
        redeemed_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
        active = MagicMock(redeemed=None)
        already_redeemed = MagicMock(
            redeemed=Redemption(dt=redeemed_at, reason="previous jump")
        )
        tickets = {
            "active": active,
            "used": already_redeemed,
            "missing": None,
        }
        ticket_class.objects.side_effect = lambda code: SimpleNamespace(
            first=lambda: tickets[code]
        )

        status, _headers, body = service._redeem(
            {"codes": "active used missing", "reason": " jump "}
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"redeemed OK", body)
        self.assertIn(b"already redeemed", body)
        self.assertIn(b"not found", body)
        self.assertIn(b"2026-08-22", body)
        self.assertIn(b"previous jump", body)
        active.save.assert_called_once_with()
        self.assertEqual(active.redeemed.dt.tzinfo, timezone.utc)
        self.assertIsNone(active.redeemed.by_user)
        self.assertEqual(active.redeemed.reason, "jump")

    @patch.object(service, "Ticket")
    def test_redeem_omits_blank_reason(self, ticket_class) -> None:
        active = MagicMock(redeemed=None)
        ticket_class.objects.return_value.first.return_value = active

        service._redeem({"codes": "active", "reason": "  "})

        self.assertIsNone(active.redeemed.reason)

    @patch.object(service, "Ticket")
    def test_issue_confirms_the_just_issued_tickets(self, ticket_class) -> None:
        tickets = [
            MagicMock(code="code-1", id="507f1f77bcf86cd799439011"),
            MagicMock(code="code-2", id="507f1f77bcf86cd799439012"),
        ]
        ticket_class.side_effect = tickets

        status, _headers, body = service._issue(
            {
                "to_display_name": "Jane",
                "count": "2",
                "payment": "cash",
                "purpose": "C182 hop-and-hop",
            },
            {"id": ObjectId("507f1f77bcf86cd7994390aa"), "display_name": "Issuer"},
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"Tickets issued: 2", body)
        self.assertIn(b"C182 hop-and-hop", body)
        self.assertIn(b"cash", body)
        self.assertNotIn(b"code-1", body)
        self.assertIn(
            b"/print?id=507f1f77bcf86cd799439011&amp;id=507f1f77bcf86cd799439012",
            body,
        )

    @patch.object(service, "Ticket")
    def test_print_renders_only_the_requested_tickets(self, ticket_class) -> None:
        ticket = MagicMock(code="secret-code", issued_to=user_ref("Jane"))
        ticket_class.objects.return_value = [ticket]

        with patch.object(service, "PDF") as pdf_class:
            status, headers, _body = service._print_tickets(["507f1f77bcf86cd799439011"])

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(("Content-Type", "application/pdf"), headers)
        ticket_class.objects.assert_called_once_with(
            id__in=[service.ObjectId("507f1f77bcf86cd799439011")]
        )
        pdf_class.return_value.append.assert_called_once_with(ticket)

    def test_print_rejects_an_invalid_ticket_id(self) -> None:
        status, headers, body = service._print_tickets(["not-an-id"])

        self.assertEqual(status, service.HTTPStatus.BAD_REQUEST)
        self.assertIn(("Content-Type", "text/html; charset=utf-8"), headers)
        self.assertIn(b"Invalid ticket identifier.", body)

    def test_print_requires_at_least_one_ticket(self) -> None:
        status, _headers, body = service._print_tickets([])

        self.assertEqual(status, service.HTTPStatus.BAD_REQUEST)
        self.assertIn(b"At least one ticket is required.", body)

    @patch.object(service, "Ticket")
    def test_print_without_matching_tickets_is_not_found(self, ticket_class) -> None:
        ticket_class.objects.return_value = []

        status, headers, body = service._print_tickets(["507f1f77bcf86cd799439011"])

        self.assertEqual(status, service.HTTPStatus.NOT_FOUND)
        self.assertIn(("Content-Type", "text/html; charset=utf-8"), headers)
        self.assertIn(b"No tickets found.", body)

    @patch.object(service, "Ticket")
    def test_print_renders_all_active_tickets_of_an_owner(self, ticket_class) -> None:
        tickets = [MagicMock(code="code-1", issued_to=user_ref("Jane")), MagicMock(code="code-2", issued_to=user_ref("Jane"))]
        ticket_class.objects.return_value = tickets

        with patch.object(service, "PDF") as pdf_class:
            status, headers, _body = service._print_tickets([], None, "Jane")

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(("Content-Type", "application/pdf"), headers)
        ticket_class.objects.assert_called_once_with(issued_to__id=None, issued_to__display_name="Jane", redeemed=None)
        self.assertEqual(
            pdf_class.return_value.append.call_args_list,
            [call(tickets[0]), call(tickets[1])],
        )

    def test_print_rejects_a_user_filter_together_with_ticket_ids(self) -> None:
        status, _headers, body = service._print_tickets(["507f1f77bcf86cd799439011"], None, "Jane")

        self.assertEqual(status, service.HTTPStatus.BAD_REQUEST)
        self.assertIn(b"Supply either ticket identifiers or a user filter, not both.", body)

    def test_print_rejects_an_empty_user_filter(self) -> None:
        status, _headers, body = service._print_tickets([], "", "")

        self.assertEqual(status, service.HTTPStatus.BAD_REQUEST)
        self.assertIn(b"At least one ticket is required.", body)

    @patch.object(service, "Ticket")
    def test_print_without_active_owner_tickets_is_not_found(self, ticket_class) -> None:
        ticket_class.objects.return_value = []

        status, _headers, body = service._print_tickets([], None, "Jane")

        self.assertEqual(status, service.HTTPStatus.NOT_FOUND)
        self.assertIn(b"No tickets found.", body)

    @patch.object(service, "Ticket")
    def test_view_owners_lists_only_owners_with_active_tickets(self, ticket_class) -> None:
        ticket_class.objects.side_effect = [
            SimpleNamespace(distinct=lambda _field: [ObjectId("507f1f77bcf86cd799439011")]),
            SimpleNamespace(only=lambda _field: SimpleNamespace(first=lambda: SimpleNamespace(issued_to=user_ref("Jane")))),
            SimpleNamespace(distinct=lambda _field: ["Guest"]),
        ]

        status, _headers, body = service._view_owners()

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"/tickets?user_id=507f1f77bcf86cd799439011", body)
        self.assertIn(b"/tickets?display_name=Guest", body)

    @patch.object(service, "Ticket")
    def test_view_owner_tickets_hides_the_ticket_code(self, ticket_class) -> None:
        ticket = MagicMock(
            payment="cash",
            purpose="C182 hop-and-hop",
            issued_by=user_ref("issuer-1"),
            issued_to=user_ref("Jane"),
            code="secret-code",
            id="507f1f77bcf86cd799439011",
        )
        ticket.issued_utc.return_value = datetime(2026, 8, 22, tzinfo=timezone.utc)
        ticket_class.objects.return_value = [ticket]

        status, _headers, body = service._view_owner_tickets(None, "Jane")

        self.assertEqual(status, service.HTTPStatus.OK)
        ticket_class.objects.assert_called_once_with(issued_to__id=None, issued_to__display_name="Jane", redeemed=None)
        self.assertIn(b"2026-08-22 00:00:00 UTC", body)
        self.assertIn(b"issuer-1", body)
        self.assertIn(b"C182 hop-and-hop", body)
        self.assertIn(b"cash", body)
        self.assertNotIn(b"secret-code", body)
        self.assertIn(b'href="/print?id=507f1f77bcf86cd799439011"', body)
        self.assertIn(b'href="/print?display_name=Jane"', body)

    def test_user_search_limits_results_to_ten(self) -> None:
        prefix_users = [SimpleNamespace(id=ObjectId(), display_name=f"Jane {index}") for index in range(7)]
        contains_users = [SimpleNamespace(id=ObjectId(), display_name=f"X Jane {index}") for index in range(5)]
        first_query = MagicMock()
        first_query.only.return_value.limit.return_value = prefix_users
        second_query = MagicMock()
        second_query.only.return_value.limit.return_value = contains_users
        user_class = MagicMock()
        user_class.objects.side_effect = [first_query, second_query]

        result = search_users("Jane", user_class=user_class)

        self.assertEqual(len(result), 10)
        self.assertEqual(first_query.only.return_value.limit.call_args.args[0], 10)
        self.assertEqual(second_query.only.return_value.limit.call_args.args[0], 3)

    @patch.object(service.mongoengine, "register_connection")
    @patch("dropzone_ticketing.service.config._file_config")
    def test_ensure_storage_uses_yaml_mongodb_uri(self, file_config, register_connection) -> None:
        original_connected = service._storage_connected
        self.addCleanup(setattr, service, "_storage_connected", original_connected)
        service._storage_connected = False
        file_config.return_value = {"mongodb_uri": "mongodb://yaml.example/test"}
        service._ensure_storage()

        register_connection.assert_called_once_with(
            service.mongoengine_alias,
            host="mongodb://yaml.example/test",
        )
        self.assertTrue(service._storage_connected)

        # A second call should be a no-op and must not reconnect.
        service._ensure_storage()
        register_connection.assert_called_once()

    @patch.object(service, "Ticket")
    def test_issue_records_issuing_user(self, ticket_class) -> None:
        ticket_class.return_value = MagicMock()

        service._issue(
            {
                "to_display_name": "Jane",
                "count": "1",
                "payment": "cash",
                "purpose": "C182 hop-and-hop",
            },
            {"id": ObjectId("507f1f77bcf86cd799439099"), "display_name": "issuer-1"},
        )

        ticket_class.assert_called_once()
        self.assertEqual(ticket_class.call_args.kwargs["issued_to"].display_name, "Jane")
        self.assertEqual(ticket_class.call_args.kwargs["issued_by"].display_name, "issuer-1")

    @patch.object(service, "Ticket")
    def test_redeem_records_redeeming_user(self, ticket_class) -> None:
        active = MagicMock(redeemed=None)
        ticket_class.objects.return_value.first.return_value = active

        service._redeem({"codes": "active"}, "redeemer-1")

        self.assertEqual(active.redeemed.by_user, "redeemer-1")

    def test_redeemed_report_groups_today_and_yesterday_by_owner(self) -> None:
        today = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        query_args = {}

        query = [
            SimpleNamespace(
                issued_to=user_ref("Jane"),
                code="today-code",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 24, 9, tzinfo=timezone.utc),
                    by_user="redeemer-1",
                    reason="jump",
                ),
            ),
            SimpleNamespace(
                issued_to=user_ref("Jane"),
                code="yesterday-code",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 23, 20, tzinfo=timezone.utc),
                    by_user=None,
                    reason=None,
                ),
            ),
        ]

        def objects(**kwargs):
            query_args.update(kwargs)
            return query

        status, _headers, body = view_redeemed_tickets(
            ticket_class=SimpleNamespace(objects=objects),
            render=service._render,
            now=today,
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertEqual(
            query_args,
            {
                "redeemed__dt__gte": datetime(2026, 8, 23, tzinfo=timezone.utc),
                "redeemed__dt__lt": datetime(2026, 8, 25, tzinfo=timezone.utc),
            },
        )
        self.assertIn(b"Today:", body)
        self.assertIn(b"Yesterday:", body)
        self.assertIn(b'<span title="Reason: jump; By: redeemer-1; At: 2026-08-24 09:00:00 UTC">1</span>', body)
        self.assertIn(b'<span title="Reason: unknown; By: unknown; At: 2026-08-23 20:00:00 UTC">1</span>', body)
        self.assertNotIn(b">today-code</span>", body)

    def test_issued_report_limits_to_last_500_and_handles_missing_redemption(self) -> None:
        class Query:
            def order_by(self, *fields):
                self.order_by_fields = fields
                return self

            def limit(self, count):
                self.limit_count = count
                return [
                    SimpleNamespace(
                        issued_to=user_ref("Jane"),
                        issued_by=user_ref("issuer-1"),
                        purpose="C182 hop-and-hop",
                        payment="cash",
                        redeemed=Redemption(
                            dt=datetime(2026, 8, 24, 9, tzinfo=timezone.utc),
                            by_user="redeemer-1",
                            reason="jump",
                        ),
                        issued_utc=lambda: ObjectId("64e3b8000000000000000000").generation_time,
                    ),
                    SimpleNamespace(
                        issued_to=user_ref("Zoe"),
                        issued_by=user_ref("issuer-2"),
                        purpose="packing",
                        payment="card",
                        redeemed=None,
                        issued_utc=lambda: ObjectId("64e3a0000000000000000000").generation_time,
                    ),
                ]

        query = Query()

        status, _headers, body = view_issued_tickets(
            ticket_class=SimpleNamespace(objects=query),
            render=service._render,
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertEqual(query.order_by_fields, ("-id",))
        self.assertEqual(query.limit_count, 500)
        self.assertIn(b"Jane", body)
        self.assertIn(b"C182 hop-and-hop", body)
        self.assertIn(b"cash", body)
        self.assertIn(b"2026-08-24 09:00:00 UTC", body)
        self.assertIn(b"redeemer-1", body)
        self.assertIn(b"jump", body)
        self.assertIn(b"Zoe", body)
        self.assertIn(b"not redeemed", body)
        self.assertIn(b"unknown", body)


class ServiceApplicationTest(unittest.TestCase):
    def request(self, path: str, method: str = "GET", form: Optional[dict] = None, authenticated: bool = True):
        body = urlencode(form or {}).encode()
        environ = {
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REQUEST_METHOD": method,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        response = {}

        def start_response(status, headers):
            response["status"] = status
            response["headers"] = dict(headers)
            response["raw_headers"] = headers

        with patch.object(service, "_ensure_storage"), patch.object(
            service._auth_module, "_is_authenticated", return_value=authenticated
        ), patch.object(
            service._auth_module,
            "current_user_ref",
            return_value={"id": ObjectId("507f1f77bcf86cd799439011"), "display_name": "Jane"} if authenticated else None,
        ):
            response["body"] = b"".join(service.application(environ, start_response))
        return response

    def test_index_is_html(self) -> None:
        response = self.request("/")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["headers"]["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Issue tickets", response["body"])
        self.assertIn(b'href="/reports/redeemed"', response["body"])
        self.assertIn(b'href="/reports/issued"', response["body"])

    def test_base_template_shows_authentication_status(self) -> None:
        response = self.request("/", authenticated=False)

        self.assertIn(b'<a href="/authn">Sign in</a>', response["body"])

    def test_base_template_shows_signed_in_user(self) -> None:
        with patch.object(service._auth_module, "current_user_id", return_value="507f1f77bcf86cd799439011"), patch.object(
            service._auth_module, "current_user_display_name", return_value="Jane"
        ):
            response = self.request("/", authenticated=True)

        self.assertIn(b'Signed in as <a href="/authn">Jane</a>', response["body"])
        self.assertNotIn(b'<a href="/authn">Sign in</a>', response["body"])

    def test_issue_rejects_out_of_range_count(self) -> None:
        response = self.request(
            "/issue",
            "POST",
            {
                "to_display_name": "Jane",
                "count": "1001",
                "payment": "cash",
                "purpose": "C182 hop-and-hop",
            },
        )

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertIn(b"between 1 and 1000", response["body"])

    def test_issue_rejects_missing_payment(self) -> None:
        response = self.request(
            "/issue",
            "POST",
            {
                "to_display_name": "Jane",
                "count": "1",
                "payment": " ",
                "purpose": "C182 hop-and-hop",
            },
        )

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertIn(b"Payment is required.", response["body"])

    def test_issue_rejects_missing_purpose(self) -> None:
        response = self.request(
            "/issue",
            "POST",
            {
                "to_display_name": "Jane",
                "count": "1",
                "payment": "cash",
                "purpose": " ",
            },
        )

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertIn(b"Purpose is required.", response["body"])

    def test_issue_requires_exactly_one_user_field(self) -> None:
        response = self.request(
            "/issue",
            "POST",
            {
                "to_id": "507f1f77bcf86cd799439011",
                "to_display_name": "Jane",
                "count": "1",
                "payment": "cash",
                "purpose": "C182 hop-and-hop",
            },
        )
        self.assertEqual(response["status"], "400 Bad Request")
        self.assertIn(b"Exactly one of to_id or to_display_name is required.", response["body"])

    def test_print_page_is_removed(self) -> None:
        response = self.request("/print")

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertNotIn(b"Print tickets", response["body"])

    def test_navigation_does_not_link_the_print_page(self) -> None:
        response = self.request("/")

        self.assertNotIn(b'href="/print"', response["body"])

    def test_report_links_are_only_on_home_page(self) -> None:
        response = self.request("/issue")

        self.assertNotIn(b'href="/reports/redeemed"', response["body"])
        self.assertNotIn(b'href="/reports/issued"', response["body"])

    def test_redeemed_report_route_uses_handler(self) -> None:
        with patch.object(service, "_view_redeemed_tickets", return_value=(service.HTTPStatus.OK, [], b"report")):
            response = self.request("/reports/redeemed")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["body"], b"report")

    def test_issued_report_route_uses_handler(self) -> None:
        with patch.object(service, "_view_issued_tickets", return_value=(service.HTTPStatus.OK, [], b"report")):
            response = self.request("/reports/issued")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["body"], b"report")

    def test_protected_routes_redirect_to_authentication(self) -> None:
        response = self.request("/issue", authenticated=False)

        self.assertEqual(response["status"], "303 See Other")
        self.assertEqual(response["headers"]["Location"], "/authn")

    def test_unknown_path_is_not_found(self) -> None:
        response = self.request("/missing")

        self.assertEqual(response["status"], "404 Not Found")

    def test_wrong_method_includes_allow_header(self) -> None:
        response = self.request("/redeem", "DELETE")

        self.assertEqual(response["status"], "405 Method Not Allowed")
        self.assertEqual(response["headers"]["Allow"], "GET, POST")

    def test_registration_mode_blocks_non_registration_routes(self) -> None:
        with patch("dropzone_ticketing.service.routes.authn_config", return_value=SimpleNamespace(register=True)):
            response = self.request("/authn", authenticated=False)

        self.assertEqual(response["status"], "403 Forbidden")
        self.assertIn(b"registration-only mode", response["body"])

    def test_registration_mode_only_serves_the_registration_route(self) -> None:
        with patch.object(config, "registration_mode", return_value=True):
            blocked = [self.request(path, authenticated=False)["status"] for path in ("/", "/issue", "/tickets")]
            registration = self.request("/register", authenticated=False)

        self.assertEqual(blocked, ["403 Forbidden"] * 3)
        self.assertEqual(registration["status"], "200 OK")

    def test_registration_mode_navigation_only_links_registration(self) -> None:
        with patch.object(config, "registration_mode", return_value=True):
            responses = [self.request(path, authenticated=False) for path in ("/", "/register")]

        for response in responses:
            self.assertIn(b'href="/register"', response["body"])
            for link in (b'href="/"', b'href="/authn"', b'href="/issue"', b'href="/redeem"', b'href="/tickets"'):
                self.assertNotIn(link, response["body"])


class ServiceAuthnTest(unittest.TestCase):
    def request(self, path: str, method: str = "GET", form: Optional[dict] = None, cookie: str = ""):
        body = urlencode(form or {}).encode()
        environ = {
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REQUEST_METHOD": method,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_HOST": "example.test",
            "HTTP_COOKIE": cookie,
            "wsgi.url_scheme": "https",
        }
        response = {}

        def start_response(status, headers):
            response["status"] = status
            response["headers"] = dict(headers)
            response["raw_headers"] = headers

        with patch.object(service, "_ensure_storage"):
            response["body"] = b"".join(service.application(environ, start_response))
        return response

    def test_authn_begin_sets_challenge_cookie_without_registration_options(self) -> None:
        auth = service._auth_module
        server = MagicMock()
        state = {"challenge": b64(b"server challenge"), "user_verification": None}
        server.authenticate_begin.return_value = {}, state
        credential = SimpleNamespace(id=b"credential")
        user = SimpleNamespace(fido2_credentials=[credential])
        user_class = MagicMock()
        user_class.objects.return_value.only.return_value = [user]
        environ = {
            "PATH_INFO": "/authn",
            "HTTP_HOST": "example.test",
            "wsgi.url_scheme": "https",
        }

        with patch.object(auth, "_server", return_value=server), patch.object(
            auth, "User", user_class
        ), patch.object(auth, "_credential_data", return_value="credential data"):
            _status, headers, body = auth.begin_authn(environ)

        server.authenticate_begin.assert_called_once_with(["credential data"], challenge=ANY)
        cookie = next(value for name, value in headers if name == "Set-Cookie")
        payload = auth._unsign(cookie.split(";", 1)[0].split("=", 1)[1])
        self.assertEqual(payload["state"], state)
        self.assertIn("Path=/authn", cookie)
        self.assertIn(b"navigator.credentials.get", body)
        self.assertIn(b"registrationOptions = null", body)
        self.assertNotIn(b"Register credential", body)

    def test_authn_complete_uses_stored_credential_and_sets_session_cookie(self) -> None:
        auth = service._auth_module
        challenge = b"challenge"
        cookie = "authn_challenge=" + auth._signed(
            {"challenge": b64(challenge), "issued": auth.time()}
        )
        server = MagicMock()
        credential = SimpleNamespace(id=b"credential")

        with patch.object(auth, "_server", return_value=server), patch.object(
            auth, "_find_credential", return_value=credential
        ), patch.object(auth, "_credential_data", return_value="credential data"):
            response = self.request(
                "/authn",
                "POST",
                {
                    "id": "credential",
                    "rawId": b64(b"credential"),
                    "clientDataJSON": b64(b"client"),
                    "authenticatorData": b64(b"authenticator"),
                    "signature": b64(b"signature"),
                },
                cookie,
            )

        self.assertEqual(response["status"], "303 See Other")
        self.assertEqual(response["headers"]["Location"], "/")
        self.assertIn("authn_session=", "\n".join(value for name, value in response["raw_headers"] if name == "Set-Cookie"))
        server.authenticate_complete.assert_called_once()

    def test_authn_complete_rejects_missing_challenge_cookie(self) -> None:
        response = self.request(
            "/authn",
            "POST",
            {"rawId": b64(b"credential")},
        )

        self.assertEqual(response["status"], "403 Forbidden")
        self.assertIn(b"Authentication challenge is missing or expired.", response["body"])

    def test_registration_begin_uses_server_options_and_persists_state(self) -> None:
        auth = register_module
        user_id = "507f1f77bcf86cd799439011"
        state = {"challenge": b64(b"server challenge"), "user_verification": "discouraged"}
        options = {
            "publicKey": {
                "challenge": b"server challenge",
                "rp": {"name": "dropzone-ticketing", "id": "example.test"},
                "user": {"id": b"user id", "name": user_id, "displayName": "Jane"},
                "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
            }
        }
        server = MagicMock()
        server.register_begin.return_value = options, state
        user_class = MagicMock()
        user_class.objects.return_value.only.return_value.first.return_value = None
        environ = {
            "PATH_INFO": "/register",
            "QUERY_STRING": f"user_id={user_id}&display_name=Jane",
            "HTTP_HOST": "example.test",
            "wsgi.url_scheme": "https",
        }

        with patch.object(auth, "authn_config", return_value=SimpleNamespace(register=True)), patch.object(
            auth, "_server", return_value=server
        ), patch.object(auth, "User", user_class):
            _status, headers, body = auth.begin_register(environ)

        registration_user, credentials = server.register_begin.call_args.args[:2]
        self.assertEqual(registration_user.name, user_id)
        self.assertEqual(registration_user.display_name, "Jane")
        self.assertEqual(registration_user.id, ObjectId(user_id).binary)
        self.assertEqual(credentials, [])
        self.assertEqual(server.register_begin.call_args.kwargs["user_verification"], "discouraged")
        cookie = next(value for name, value in headers if name == "Set-Cookie")
        payload = auth._unsign(cookie.split(";", 1)[0].split("=", 1)[1])
        self.assertEqual(payload["state"], state)
        self.assertEqual(payload["user_id"], user_id)
        self.assertIn(b64(b"server challenge").encode(), body)
        self.assertNotIn(b"crypto.getRandomValues", body)

    def test_registration_begin_renders_generated_user_id_when_missing(self) -> None:
        auth = register_module
        environ = {
            "PATH_INFO": "/register",
            "QUERY_STRING": "",
            "HTTP_HOST": "example.test",
            "wsgi.url_scheme": "https",
        }

        with patch.object(auth, "authn_config", return_value=SimpleNamespace(register=True)):
            _status, _headers, body = auth.begin_register(environ)

        self.assertRegex(
            body.decode(),
            r'<input type="hidden" name="user_id" id="user_id" value="[0-9a-f]{24}">',
        )

    def test_registration_complete_uses_state_from_cookie(self) -> None:
        auth = register_module
        user_id = "507f1f77bcf86cd799439012"
        state = {"challenge": b64(b"server challenge"), "user_verification": "discouraged"}
        cookie = "authn_challenge=" + auth._signed(
            {"state": state, "user_id": user_id, "issued": auth.time()}
        )
        class CredentialData:
            credential_id = b"credential"
            aaguid = b"\x01" * 16
            public_key = {1: 2, 3: -7}

            def __bytes__(self):
                return b"serialized credential"

        credential_data = CredentialData()
        server = MagicMock()
        server.register_complete.return_value = SimpleNamespace(
            credential_data=credential_data, extensions={"credProps": {"rk": True}}
        )
        user = MagicMock()
        user.fido2_credentials = []
        user_class = MagicMock()
        user_class.objects.return_value.first.return_value = None
        user_class.return_value = user
        environ = {
            "PATH_INFO": "/register",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(
                urlencode(
                    {
                        "user_id": user_id,
                        "display_name": "Jane Sky",
                        "id": "credential",
                        "rawId": b64(b"credential"),
                        "clientDataJSON": b64(b"client data"),
                        "attestationObject": b64(b"attestation"),
                    }
                ).encode()
            ),
            "HTTP_COOKIE": cookie,
        }
        environ["CONTENT_LENGTH"] = str(environ["wsgi.input"].getbuffer().nbytes)

        with patch.object(auth, "authn_config", return_value=SimpleNamespace(register=True)), patch.object(
            auth, "_server", return_value=server
        ), patch.object(auth, "User", user_class), patch.object(
            auth, "_find_credential", return_value=None
        ), patch.object(auth, "CollectedClientData", return_value="client data"), patch.object(
            auth, "AttestationObject", return_value="attestation"
        ), patch.object(
            auth, "AuthenticatorAttestationResponse", return_value="attestation response"
        ), patch.object(auth, "RegistrationResponse", return_value="registration response") as registration_response:
            status, _headers, _body = auth.complete_register(environ)

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        self.assertEqual(server.register_complete.call_args.args[0], state)
        self.assertEqual(server.register_complete.call_args.kwargs["response"], "registration response")
        registration_response.assert_called_once()
        user_class.objects.assert_called_once_with(id=ObjectId(user_id))
        user_class.assert_called_once_with(id=ObjectId(user_id), display_name="Jane Sky")
        credential = user.fido2_credentials[0]
        self.assertEqual(credential.attestation_aaguid, b"\x01" * 16)
        self.assertEqual(credential.extensions, {"credProps": {"rk": True}})
        user.save.assert_called_once_with()

    def test_authn_begin_shows_registered_credentials_for_signed_in_user(self) -> None:
        auth = service._auth_module
        server = MagicMock()
        state = {"challenge": b64(b"server challenge"), "user_verification": None}
        register_state = {"challenge": b64(b"register challenge"), "user_verification": "discouraged"}
        server.authenticate_begin.return_value = {}, state
        server.register_begin.return_value = {"publicKey": {"challenge": b"register challenge"}}, register_state
        user = SimpleNamespace(
            id=ObjectId("507f1f77bcf86cd799439011"),
            display_name="Jane",
            fido2_credentials=[
                SimpleNamespace(
                    id=b"abcdefgh",
                    dt=datetime(2026, 8, 22, tzinfo=timezone.utc),
                    attestation_aaguid=bytes(range(16)),
                    extensions={"credProps": {"rk": True}},
                )
            ],
        )
        environ = {
            "PATH_INFO": "/authn",
            "HTTP_HOST": "example.test",
            "wsgi.url_scheme": "https",
        }

        with patch.object(auth, "_server", return_value=server), patch.object(
            auth, "User", MagicMock(objects=MagicMock(return_value=MagicMock(only=MagicMock(return_value=[]))))
        ), patch.object(auth, "_session_user", return_value=user), patch.object(
            auth, "_credential_data", return_value="credential data"
        ):
            _status, headers, body = auth.begin_authn(environ)

        self.assertIn(b"My FIDO2 credentials", body)
        self.assertIn(b"61626364", body)
        self.assertIn(b"65666768", body)
        self.assertIn(b">Add another</button>", body)
        self.assertIn(b"00010203-0405-0607-0809-0a0b0c0d0e0f", body)
        self.assertIn(b"&#34;rk&#34;: true", body)
        cookie = next(value for name, value in headers if name == "Set-Cookie")
        payload = auth._unsign(cookie.split(";", 1)[0].split("=", 1)[1])
        self.assertEqual(payload["register_state"], register_state)
        self.assertEqual(payload["register_user"], "507f1f77bcf86cd799439011")

    def test_authn_register_complete_attaches_credential_to_current_user(self) -> None:
        auth = service._auth_module
        state = {"challenge": b64(b"server challenge"), "user_verification": "discouraged"}
        cookie = "authn_challenge=" + auth._signed(
            {"register_state": state, "register_user": "507f1f77bcf86cd799439011", "issued": auth.time()}
        )

        class CredentialData:
            credential_id = b"credential"
            aaguid = b"\x01" * 16
            public_key = {1: 2, 3: -7}

            def __bytes__(self):
                return b"serialized credential"

        credential_data = CredentialData()
        server = MagicMock()
        server.register_complete.return_value = SimpleNamespace(
            credential_data=credential_data, extensions={"credProps": {"rk": True}}
        )
        user = MagicMock(id=ObjectId("507f1f77bcf86cd799439011"), fido2_credentials=[])
        environ = {
            "PATH_INFO": "/authn/register",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(
                urlencode(
                    {
                        "id": "credential",
                        "rawId": b64(b"credential"),
                        "clientDataJSON": b64(b"client data"),
                        "attestationObject": b64(b"attestation"),
                    }
                ).encode()
            ),
            "HTTP_COOKIE": cookie,
        }
        environ["CONTENT_LENGTH"] = str(environ["wsgi.input"].getbuffer().nbytes)

        with patch.object(auth, "authn_config", return_value=SimpleNamespace(register=False)), patch.object(
            auth, "_session_user", return_value=user
        ), patch.object(auth, "_server", return_value=server), patch.object(
            auth, "_find_credential", return_value=None
        ), patch.object(auth, "CollectedClientData", return_value="client data"), patch.object(
            auth, "AttestationObject", return_value="attestation"
        ), patch.object(
            auth, "AuthenticatorAttestationResponse", return_value="attestation response"
        ), patch.object(auth, "RegistrationResponse", return_value="registration response"):
            response = auth.complete_authn_register(environ)

        self.assertEqual(response[0], service.HTTPStatus.SEE_OTHER)
        self.assertEqual(user.fido2_credentials[0].id, b"credential")
        self.assertEqual(user.fido2_credentials[0].data, b"serialized credential")
        credential = user.fido2_credentials[0]
        self.assertEqual(credential.attestation_aaguid, b"\x01" * 16)
        self.assertEqual(credential.extensions, {"credProps": {"rk": True}})
        user.save.assert_called_once_with()

    def test_authn_remove_fido2_credential_requires_csrf_and_persists(self) -> None:
        auth = service._auth_module
        credential = SimpleNamespace(id=b"credential")
        user = MagicMock(fido2_credentials=[credential])
        body = urlencode({"credential_id": b64(b"credential"), "csrf": "token"}).encode()
        environ = {
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_COOKIE": "google_csrf=token",
        }

        with patch.object(auth, "_session_user", return_value=user):
            status, headers, _body = auth.remove_fido2_credential(environ)

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        self.assertEqual(dict(headers)["Location"], "/authn")
        self.assertEqual(user.fido2_credentials, [])
        user.save.assert_called_once_with()

    def test_authn_displays_aaguid_as_uuid(self) -> None:
        auth = service._auth_module
        self.assertEqual(
            auth._aaguid_display(SimpleNamespace(attestation_aaguid=bytes(range(16)))),
            "00010203-0405-0607-0809-0a0b0c0d0e0f",
        )
        self.assertEqual(auth._aaguid_display(SimpleNamespace(attestation_aaguid=b"\x01\x02")), "0102")
        self.assertIsNone(auth._aaguid_display(SimpleNamespace()))
        self.assertIsNone(auth._aaguid_display(SimpleNamespace(attestation_aaguid=b"")))

    def test_authn_displays_extensions_as_json(self) -> None:
        auth = service._auth_module
        self.assertEqual(
            auth._extensions_display(SimpleNamespace(extensions={"credProps": {"rk": True}, "raw": b"\x01"})),
            '{\n  "credProps": {\n    "rk": true\n  },\n  "raw": "AQ"\n}',
        )
        self.assertIsNone(auth._extensions_display(SimpleNamespace()))
        self.assertIsNone(auth._extensions_display(SimpleNamespace(extensions={})))

    def test_registration_fields_tolerate_missing_authenticator_data(self) -> None:
        auth = service._auth_module
        self.assertEqual(auth._registration_fields(SimpleNamespace(credential_data=None, extensions=None)), {})
        self.assertEqual(
            auth._registration_fields(
                SimpleNamespace(
                    credential_data=SimpleNamespace(aaguid=b"\x01" * 16, public_key={1: 2}),
                    extensions={"credProps": {"rk": False}},
                )
            ),
            {
                "attestation_aaguid": b"\x01" * 16,
                "extensions": {"credProps": {"rk": False}},
            },
        )

    def test_display_name_update_persists_for_authenticated_user(self) -> None:
        auth = service._auth_module
        user = MagicMock(display_name=None)
        environ = {
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(urlencode({"display_name": "  Jane Sky  "}).encode()),
            "HTTP_COOKIE": "authn_session=signed",
        }
        environ["CONTENT_LENGTH"] = str(environ["wsgi.input"].getbuffer().nbytes)
        with patch.object(auth, "_session_user", return_value=user):
            status, headers, _body = auth.update_display_name(environ)

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        self.assertEqual(dict(headers)["Location"], "/authn")
        self.assertEqual(user.display_name, "Jane Sky")
        user.save.assert_called_once_with()


class ServiceGoogleTest(unittest.TestCase):
    def setUp(self) -> None:
        google_module._endpoints.cache_clear()
        self.addCleanup(google_module._endpoints.cache_clear)

    def environ(self, query: str = "", cookie: str = "") -> dict:
        return {
            "PATH_INFO": "/authn/google",
            "QUERY_STRING": query,
            "REQUEST_METHOD": "GET",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
            "HTTP_HOST": "example.test",
            "HTTP_COOKIE": cookie,
            "wsgi.url_scheme": "https",
        }

    def test_endpoints_are_read_from_the_discovery_document(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "authorization_endpoint": "https://accounts.test/auth",
            "token_endpoint": "https://tokens.test/token",
        }
        with patch.object(google_module.requests, "get", return_value=response) as get:
            self.assertEqual(
                google_module._endpoints(),
                ("https://accounts.test/auth", "https://tokens.test/token"),
            )
            self.assertEqual(google_module._endpoints(), ("https://accounts.test/auth", "https://tokens.test/token"))
        get.assert_called_once_with(google_module._GOOGLE_DISCOVERY_URI, timeout=10)

    def test_oauth_flow_requests_only_the_email_scope(self) -> None:
        with patch.object(
            google_module, "_endpoints", return_value=("https://accounts.test/auth", "https://tokens.test/token")
        ), patch.object(google_module, "google_client_id", return_value="client"), patch.object(
            google_module, "google_client_secret", return_value="secret"
        ), patch.object(google_module.Flow, "from_client_config") as from_client_config:
            google_module._oauth_flow("https://example.test/authn/google/callback", "verifier")

        config, = from_client_config.call_args.args
        self.assertEqual(config["web"]["client_id"], "client")
        self.assertEqual(config["web"]["auth_uri"], "https://accounts.test/auth")
        self.assertEqual(config["web"]["token_uri"], "https://tokens.test/token")
        self.assertEqual(from_client_config.call_args.kwargs["scopes"], ["email"])
        self.assertEqual(
            from_client_config.call_args.kwargs["redirect_uri"], "https://example.test/authn/google/callback"
        )

    def test_begin_redirects_to_the_authorization_url(self) -> None:
        flow = MagicMock()
        flow.authorization_url.return_value = ("https://accounts.test/auth?state=abc", "abc")
        with patch.object(google_module, "_configured", return_value=True), patch.object(
            google_module, "_session_user", return_value=None
        ), patch.object(google_module, "_oauth_flow", return_value=flow):
            status, headers, _body = google_module.begin(self.environ())

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        self.assertEqual(dict(headers)["Location"], "https://accounts.test/auth?state=abc")
        state_cookie = next(value for name, value in headers if name == "Set-Cookie" and "google_oauth_state=" in value)
        self.assertIn("SameSite=Lax", state_cookie)

    def test_complete_logs_in_an_existing_user_using_the_userinfo_profile(self) -> None:
        flow = MagicMock()
        service_client = MagicMock()
        service_client.userinfo.return_value.get.return_value.execute.return_value = {
            "email": "Jane@Example.test",
            "verified_email": True,
        }
        user = MagicMock(id="Jane")
        with patch.object(
            google_module, "_state", return_value={"state": "abc", "user": None, "code_verifier": "verifier"}
        ), patch.object(
            google_module, "_oauth_flow", return_value=flow
        ), patch.object(google_module, "build", return_value=service_client) as build, patch.object(
            google_module, "_session_user", return_value=None
        ), patch.object(google_module, "User") as user_class:
            user_class.objects.return_value.first.return_value = user
            status, headers, _body = google_module.complete(self.environ(query="state=abc&code=auth-code"))

        flow.fetch_token.assert_called_once_with(code="auth-code")
        build.assert_called_once_with("oauth2", "v2", credentials=flow.credentials)
        user_class.objects.assert_called_once_with(google_credentials__email="jane@example.test")
        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        self.assertEqual(dict(headers)["Location"], "/authn")
        cleared_state_cookie = next(
            value for name, value in headers if name == "Set-Cookie" and "google_oauth_state=" in value
        )
        self.assertIn("SameSite=Lax", cleared_state_cookie)
        session_cookie = next(value for name, value in headers if name == "Set-Cookie" and "authn_session=" in value)
        self.assertIn("SameSite=Lax", session_cookie)

    def test_complete_rejects_an_unverified_email(self) -> None:
        flow = MagicMock()
        service_client = MagicMock()
        service_client.userinfo.return_value.get.return_value.execute.return_value = {
            "email": "jane@example.test",
            "verified_email": False,
        }
        with patch.object(google_module, "_state", return_value={"state": "abc", "user": None, "code_verifier": "verifier"}), patch.object(
            google_module, "_oauth_flow", return_value=flow
        ), patch.object(google_module, "build", return_value=service_client):
            status, _headers, _body = google_module.complete(self.environ(query="state=abc&code=auth-code"))

        self.assertEqual(status, service.HTTPStatus.FORBIDDEN)

    def test_complete_reports_a_failed_token_exchange(self) -> None:
        flow = MagicMock()
        flow.fetch_token.side_effect = GoogleAuthError("boom")
        with patch.object(google_module, "_state", return_value={"state": "abc", "user": None, "code_verifier": "verifier"}), patch.object(
            google_module, "_oauth_flow", return_value=flow
        ):
            status, _headers, body = google_module.complete(self.environ(query="state=abc&code=auth-code"))

        self.assertEqual(status, service.HTTPStatus.FORBIDDEN)
        self.assertIn(b"GoogleAuthError", body)
        self.assertIn(b"boom", body)
        self.assertIn(b"Traceback (most recent call last)", body)

    def test_begin_reports_a_failed_authorization_url(self) -> None:
        with patch.object(google_module, "_configured", return_value=True), patch.object(
            google_module, "_session_user", return_value=None
        ), patch.object(google_module, "_oauth_flow", side_effect=GoogleAuthError("boom")):
            status, _headers, body = google_module.begin(self.environ())

        self.assertEqual(status, service.HTTPStatus.FORBIDDEN)
        self.assertIn(b"GoogleAuthError", body)
        self.assertIn(b"boom", body)
        self.assertIn(b"Traceback (most recent call last)", body)


class ServiceConfigTest(unittest.TestCase):
    def patch_config(self, values: dict) -> None:
        patcher = patch.object(config, "_file_config", return_value=values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_are_read_from_the_yaml_file(self) -> None:
        self.patch_config(
            {
                "mongodb_uri": "mongodb://yaml.example/test",
                "registration_mode": True,
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.mongodb_uri(), "mongodb://yaml.example/test")
            self.assertTrue(config.registration_mode())

    def test_the_environment_does_not_provide_settings(self) -> None:
        self.patch_config({"mongodb_uri": "mongodb://yaml.example/test"})
        environment = {
            "MONGODB_URI": "mongodb://env.example/test",
            "REGISTRATION_MODE": "1",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(config.mongodb_uri(), "mongodb://yaml.example/test")
            self.assertFalse(config.registration_mode())

    def test_the_session_secret_is_random_and_not_configurable(self) -> None:
        self.patch_config({"authn_session_secret": "shhh"})
        with patch.dict(os.environ, {}, clear=True):
            secret = config.session_secret()
        self.assertNotEqual(secret, b"shhh")
        self.assertEqual(len(secret), 32)
        self.assertEqual(config.session_secret(), secret)

    def test_missing_mongodb_uri_is_reported(self) -> None:
        self.patch_config({})
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                config.mongodb_uri()

    def test_google_settings_are_read_from_the_google_section(self) -> None:
        self.patch_config({"google": {"client_id": "client", "secret": "shhh"}})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.google_client_id(), "client")
            self.assertEqual(config.google_client_secret(), "shhh")

    def test_a_non_mapping_google_section_is_ignored(self) -> None:
        self.patch_config({"google": "nonsense"})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.google_client_id(), "")
            self.assertEqual(
                config.google_redirect_uri({"wsgi.url_scheme": "https", "HTTP_HOST": "example.test"}),
                "https://example.test/authn/google/callback",
            )


if __name__ == "__main__":
    unittest.main()
