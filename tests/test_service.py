from __future__ import annotations

import base64
import io
import json
import os
import traceback
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import ANY, MagicMock, call, patch
from urllib.parse import parse_qs, urlencode

from bson import ObjectId
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID
from fido2.webauthn import AttestationConveyancePreference
from google.auth.exceptions import GoogleAuthError
from mongoengine.errors import NotUniqueError

from dropzone_ticketing import service
from dropzone_ticketing.model.ticket import Redemption, UserRef
from dropzone_ticketing.service import api as api_module
from dropzone_ticketing.service.api import _shared as api_shared
from dropzone_ticketing.service.api import report as api_report_module
from dropzone_ticketing.service.api import ticket as api_ticket_module
from dropzone_ticketing.service import config
from dropzone_ticketing.service import _fido2 as fido2_module
from dropzone_ticketing.service import google as google_module
from dropzone_ticketing.service import microsoft as microsoft_module
from dropzone_ticketing.service import register as register_module
from dropzone_ticketing.service.actions.admin_users import create_user, list_users, update_user, view_user
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

    def test_admin_create_user_assigns_selected_role_and_email_identity(self) -> None:
        user = MagicMock(id=ObjectId("507f1f77bcf86cd799439011"))
        user.google_credentials = []
        user.microsoft_credentials = []
        user_class = MagicMock(return_value=user)
        render = MagicMock()

        status, headers, body = create_user(
            {
                "name": "Jane",
                "email": "JANE@EXAMPLE.TEST",
                "identity_type": "google",
                "role": "admin",
            },
            user_class=user_class,
            google_credential_class=service._auth_module.GoogleCredential,
            microsoft_credential_class=service._auth_module.MicrosoftCredential,
            render=render,
        )

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        self.assertEqual(headers, [("Location", "/admin/user/view/507f1f77bcf86cd799439011")])
        self.assertEqual(body, b"")
        user_class.assert_called_once_with(display_name="Jane", roles=["admin"])
        self.assertEqual(user.google_credentials[0].email, "jane@example.test")
        self.assertEqual(user.microsoft_credentials, [])
        user.save.assert_called_once_with()

    def test_admin_create_user_rejects_unknown_role(self) -> None:
        render = MagicMock(return_value=("rendered", [], b""))

        create_user(
            {
                "name": "Jane",
                "email": "jane@example.test",
                "identity_type": "email",
                "role": "owner",
            },
            user_class=MagicMock(),
            google_credential_class=MagicMock(),
            microsoft_credential_class=MagicMock(),
            render=render,
        )

        self.assertEqual(render.call_args.args[1], service.HTTPStatus.BAD_REQUEST)
        self.assertEqual(render.call_args.kwargs["error"], "Choose a role.")

    def test_admin_create_user_supports_regular_and_microsoft_email_identities(self) -> None:
        for identity_type in ("email", "microsoft"):
            with self.subTest(identity_type=identity_type):
                user = MagicMock(id=ObjectId("507f1f77bcf86cd799439011"))
                user.google_credentials = []
                user.microsoft_credentials = []
                user_class = MagicMock(return_value=user)

                create_user(
                    {
                        "name": "Jane",
                        "email": "jane@example.test",
                        "identity_type": identity_type,
                        "role": "solo",
                    },
                    user_class=user_class,
                    google_credential_class=service._auth_module.GoogleCredential,
                    microsoft_credential_class=service._auth_module.MicrosoftCredential,
                    render=MagicMock(),
                )

                if identity_type == "email":
                    self.assertEqual(user.email, "jane@example.test")
                    self.assertEqual(user.microsoft_credentials, [])
                else:
                    self.assertEqual(user.microsoft_credentials[0].email, "jane@example.test")

    def test_admin_view_user_returns_full_identity_details(self) -> None:
        user = SimpleNamespace(
            id=ObjectId("507f1f77bcf86cd799439011"),
            display_name="Jane",
            email="jane@example.test",
            roles=["admin"],
            fido2_credentials=[
                SimpleNamespace(
                    id=b"credential",
                    dt=datetime(2026, 8, 29, tzinfo=timezone.utc),
                    attestation_aaguid=None,
                    extensions=None,
                )
            ],
            google_credentials=[SimpleNamespace(email="google@example.test")],
            microsoft_credentials=[SimpleNamespace(email="microsoft@example.test")],
        )
        user_class = MagicMock()
        user_class.objects.return_value.first.return_value = user

        status, _headers, body = view_user(
            str(user.id),
            user_class=user_class,
            render=service._render,
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"jane@example.test", body)
        self.assertIn(b"google@example.test", body)
        self.assertIn(b"microsoft@example.test", body)
        self.assertIn(b'value="63726564656e7469616c"', body)
        self.assertIn(b"admin", body)

    def test_admin_user_list_shows_profiles_and_credentials(self) -> None:
        user = SimpleNamespace(
            id=ObjectId("507f1f77bcf86cd799439011"),
            display_name="Jane",
            email="jane@example.test",
            roles=["admin"],
            fido2_credentials=[SimpleNamespace(id=b"credential")],
            google_credentials=[SimpleNamespace(email="google@example.test")],
            microsoft_credentials=[SimpleNamespace(email="microsoft@example.test")],
        )
        users = MagicMock()
        users.order_by.return_value = [user]
        user_class = MagicMock()
        user_class.objects.return_value = users

        status, _headers, body = list_users(user_class=user_class, render=service._render)

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b'href="/admin/user/view/507f1f77bcf86cd799439011"', body)
        for expected in (b"Jane", b"admin", b"jane@example.test", b"google@example.test", b"microsoft@example.test"):
            self.assertIn(expected, body)
        self.assertIn(b'jquery-ui@1.14.1', body)
        self.assertIn(b'class="external-credentials"', body)
        self.assertIn(b'.accordion({ collapsible: true, active: false })', body)
        self.assertGreater(body.index(b'href="/admin/user/new">Add user</a>'), body.index(b"</table>"))

    def test_admin_updates_user_profile_without_email_validation_flow(self) -> None:
        user = MagicMock(id=ObjectId("507f1f77bcf86cd799439011"))
        user_class = MagicMock()
        user_class.objects.return_value.first.return_value = user

        status, headers, _body = update_user(
            str(user.id),
            {
                "action": "update",
                "name": "Jane Updated",
                "email": "NEW@EXAMPLE.TEST",
                "role": "solo",
            },
            user_class=user_class,
            render=MagicMock(),
        )

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        self.assertEqual(headers, [("Location", f"/admin/user/view/{user.id}")])
        self.assertEqual(user.display_name, "Jane Updated")
        self.assertEqual(user.email, "new@example.test")
        self.assertEqual(user.roles, ["solo"])
        user.save.assert_called_once_with()

    def test_admin_removes_external_credentials(self) -> None:
        for action, collection in (
            ("remove_google", "google_credentials"),
            ("remove_microsoft", "microsoft_credentials"),
        ):
            with self.subTest(action=action):
                user = MagicMock(id=ObjectId("507f1f77bcf86cd799439011"))
                setattr(
                    user,
                    collection,
                    [
                        SimpleNamespace(email="remove@example.test"),
                        SimpleNamespace(email="keep@example.test"),
                    ],
                )
                user_class = MagicMock()
                user_class.objects.return_value.first.return_value = user

                update_user(
                    str(user.id),
                    {"action": action, "credential": "remove@example.test"},
                    user_class=user_class,
                    render=MagicMock(),
                )

                self.assertEqual(
                    [credential.email for credential in getattr(user, collection)],
                    ["keep@example.test"],
                )
                user.save.assert_called_once_with()

    def test_admin_removes_fido2_credential(self) -> None:
        user = MagicMock(id=ObjectId("507f1f77bcf86cd799439011"))
        user.fido2_credentials = [
            SimpleNamespace(id=b"remove"),
            SimpleNamespace(id=b"keep"),
        ]
        user_class = MagicMock()
        user_class.objects.return_value.first.return_value = user

        update_user(
            str(user.id),
            {"action": "remove_fido2", "credential": b"remove".hex()},
            user_class=user_class,
            render=MagicMock(),
        )

        self.assertEqual(
            [credential.id for credential in user.fido2_credentials],
            [b"keep"],
        )
        user.save.assert_called_once_with()

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
            redeemed=Redemption(
                dt=redeemed_at,
                by=user_ref("previous-redeemer", object_id="507f1f77bcf86cd7994390ab"),
                reason="previous jump",
            )
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
            {"codes": "active used missing", "reason": " jump "},
            {"id": ObjectId("507f1f77bcf86cd7994390aa"), "display_name": "redeemer-1", "roles": ["admin"]},
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"redeemed OK", body)
        self.assertIn(b"already redeemed", body)
        self.assertIn(b"not found", body)
        self.assertIn(b"2026-08-22", body)
        self.assertIn(b"previous-redeemer", body)
        self.assertIn(b"previous jump", body)
        active.save.assert_called_once_with()
        self.assertEqual(active.redeemed.dt.tzinfo, timezone.utc)
        self.assertEqual(active.redeemed.by.id, ObjectId("507f1f77bcf86cd7994390aa"))
        self.assertEqual(active.redeemed.by.display_name, "redeemer-1")
        self.assertEqual(active.redeemed.reason, "jump")

    @patch.object(service, "Ticket")
    def test_redeem_omits_blank_reason(self, ticket_class) -> None:
        active = MagicMock(redeemed=None)
        ticket_class.objects.return_value.first.return_value = active

        service._redeem(
            {"codes": "active", "reason": "  "},
            {"id": ObjectId("507f1f77bcf86cd7994390aa"), "display_name": "redeemer-1", "roles": ["admin"]},
        )

        self.assertIsNone(active.redeemed.reason)

    @patch.object(service, "Ticket")
    def test_solo_user_cannot_redeem_another_users_ticket(self, ticket_class) -> None:
        active = MagicMock(
            redeemed=None,
            issued_to=user_ref("Other", object_id="507f1f77bcf86cd799439099"),
        )
        ticket_class.objects.return_value.first.return_value = active

        status, _headers, body = service._redeem(
            {"codes": "active"},
            {
                "id": ObjectId("507f1f77bcf86cd7994390aa"),
                "display_name": "Solo",
                "roles": ["solo"],
            },
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"not permitted", body)
        active.save.assert_not_called()

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
        self.assertEqual(pdf_class.call_args.kwargs["local_timezone"].key, "UTC")
        self.assertEqual(pdf_class.call_args.kwargs["business_name"], "The Dropzone")
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
        self.assertEqual(pdf_class.call_args.kwargs["local_timezone"].key, "UTC")
        self.assertEqual(pdf_class.call_args.kwargs["business_name"], "The Dropzone")
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

        with service.request_context(current_user_roles=["admin"]):
            status, _headers, body = service._view_owner_tickets(None, "Jane")

        self.assertEqual(status, service.HTTPStatus.OK)
        ticket_class.objects.assert_called_once_with(issued_to__id=None, issued_to__display_name="Jane", redeemed=None)
        self.assertIn(b"2026-08-22 00:00:00", body)
        self.assertNotIn(b"2026-08-22 00:00:00 UTC", body)
        self.assertIn(b"issuer-1", body)
        self.assertIn(b"C182 hop-and-hop", body)
        self.assertIn(b"cash", body)
        self.assertNotIn(b"secret-code", body)
        self.assertIn(b'href="/ticket/507f1f77bcf86cd799439011"', body)
        self.assertIn(b'href="/print?display_name=Jane"', body)
        self.assertIn(b"Back to the owners list", body)

    @patch.object(service, "Ticket")
    def test_solo_owner_tickets_hide_admin_links(self, ticket_class) -> None:
        ticket = MagicMock(
            payment="cash",
            purpose="jump",
            issued_by=user_ref("issuer"),
            issued_to=user_ref("Jane"),
            id="507f1f77bcf86cd799439011",
        )
        ticket.issued_utc.return_value = datetime(2026, 8, 22, tzinfo=timezone.utc)
        ticket_class.objects.return_value = [ticket]

        with service.request_context(current_user_roles=["solo"]):
            status, _headers, body = service._view_owner_tickets(
                "507f1f77bcf86cd799439011",
                None,
            )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertNotIn(b'href="/print?', body)
        self.assertNotIn(b"Back to the owners list", body)

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
            tz_aware=True,
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

        service._redeem(
            {"codes": "active"},
            {"id": ObjectId("507f1f77bcf86cd7994390aa"), "display_name": "redeemer-1", "roles": ["admin"]},
        )

        self.assertEqual(active.redeemed.by.id, ObjectId("507f1f77bcf86cd7994390aa"))
        self.assertEqual(active.redeemed.by.display_name, "redeemer-1")

    def test_redeemed_report_groups_today_and_yesterday_by_owner(self) -> None:
        today = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        query_args = {}

        query = [
            SimpleNamespace(
                issued_to=user_ref("Jane"),
                id=ObjectId("64e3b8000000000000000000"),
                code="today-code",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 24, 9, tzinfo=timezone.utc),
                    by=user_ref("redeemer-1", object_id="507f1f77bcf86cd7994390aa"),
                    reason="jump",
                ),
            ),
            SimpleNamespace(
                issued_to=user_ref("Jane"),
                id=ObjectId("64e3a0000000000000000000"),
                code="yesterday-code",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 23, 20, tzinfo=timezone.utc),
                    by=user_ref("redeemer-2", object_id="507f1f77bcf86cd7994390ab"),
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
        self.assertIn(
            b'<a href="/ticket/64e3b8000000000000000000" title="Reason: jump; By: redeemer-1; At: 2026-08-24 09:00:00">1</a>',
            body,
        )
        self.assertIn(
            b'<a href="/ticket/64e3a0000000000000000000" title="Reason: ; By: redeemer-2; At: 2026-08-23 20:00:00">1</a>',
            body,
        )
        self.assertNotIn(b">today-code</span>", body)
        self.assertNotIn(b"(UTC)", body)

    def test_redeemed_report_uses_configured_local_day_boundaries(self) -> None:
        today = datetime(2026, 8, 24, 1, tzinfo=timezone.utc)
        query_args = {}

        def objects(**kwargs):
            query_args.update(kwargs)
            return []

        with patch.object(config, "_file_config", return_value={"timezone": "America/Los_Angeles"}):
            status, _headers, _body = view_redeemed_tickets(
                ticket_class=SimpleNamespace(objects=objects),
                render=service._render,
                now=today,
            )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertEqual(
            query_args,
            {
                "redeemed__dt__gte": datetime(2026, 8, 22, 7, tzinfo=timezone.utc),
                "redeemed__dt__lt": datetime(2026, 8, 24, 7, tzinfo=timezone.utc),
            },
        )

    def test_issued_report_limits_to_last_500_and_handles_missing_redemption(self) -> None:
        class Query:
            def order_by(self, *fields):
                self.order_by_fields = fields
                return self

            def limit(self, count):
                self.limit_count = count
                return [
                    SimpleNamespace(
                        id=ObjectId("64e3b8000000000000000000"),
                        issued_to=user_ref("Jane", object_id="507f1f77bcf86cd799439011"),
                        issued_by=user_ref("issuer-1", object_id="507f1f77bcf86cd799439012"),
                        purpose="C182 hop-and-hop",
                        payment="cash",
                        redeemed=Redemption(
                            dt=datetime(2026, 8, 24, 9, tzinfo=timezone.utc),
                            by=user_ref("redeemer-1", object_id="507f1f77bcf86cd799439013"),
                            reason="jump",
                        ),
                        issued_utc=lambda: ObjectId("64e3b8000000000000000000").generation_time,
                    ),
                    SimpleNamespace(
                        id=ObjectId("64e3a0000000000000000000"),
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
        self.assertIn(b"2026-08-24 09:00:00", body)
        self.assertNotIn(b"2026-08-24 09:00:00 UTC", body)
        self.assertIn(b"redeemer-1", body)
        self.assertIn(b"jump", body)
        self.assertIn(b"Zoe", body)
        self.assertIn(b'href="/ticket/64e3b8000000000000000000"', body)
        self.assertIn(b'href="/tickets?user_id=507f1f77bcf86cd799439011"', body)
        self.assertIn(b'href="/tickets?user_id=507f1f77bcf86cd799439012"', body)
        self.assertIn(b'href="/tickets?user_id=507f1f77bcf86cd799439013"', body)
        self.assertIn(b'href="/tickets?display_name=Zoe"', body)
        self.assertNotIn(b"not redeemed", body)
        self.assertNotIn(b"unknown", body)

    @patch.object(service, "Ticket")
    def test_view_ticket_shows_full_details_and_pdf_link(self, ticket_class) -> None:
        ticket = SimpleNamespace(
            id=ObjectId("64e3b8000000000000000000"),
            code="secret-code",
            issued_to=user_ref("Jane", object_id="507f1f77bcf86cd799439011"),
            issued_by=user_ref("issuer-1", object_id="507f1f77bcf86cd799439012"),
            purpose="C182 hop-and-hop",
            payment="cash",
            redeemed=Redemption(
                dt=datetime(2026, 8, 24, 9, tzinfo=timezone.utc),
                by=user_ref("redeemer-1", object_id="507f1f77bcf86cd799439013"),
                reason="jump",
            ),
            issued_utc=lambda: datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        ticket_class.objects.return_value.first.return_value = ticket

        with patch.object(config, "_file_config", return_value={"timezone": "America/Los_Angeles"}):
            status, _headers, body = service._view_ticket(
                "64e3b8000000000000000000",
                {"roles": ["admin"]},
            )

        self.assertEqual(status, service.HTTPStatus.OK)
        ticket_class.objects.assert_called_once_with(id=ObjectId("64e3b8000000000000000000"))
        for expected in (b"secret-code", b"Jane", b"issuer-1", b"C182 hop-and-hop", b"cash", b"redeemer-1", b"jump"):
            self.assertIn(expected, body)
        self.assertIn(b"<title>Ticket</title>", body)
        self.assertNotIn(b"<title>Ticket 64e3b8000000000000000000</title>", body)
        self.assertIn(b"2026-08-22 17:00:00", body)
        self.assertIn(b"2026-08-24 02:00:00", body)
        self.assertNotIn(b" UTC", body)
        self.assertIn(b'href="/print?id=64e3b8000000000000000000"', body)

    @patch.object(service, "Ticket")
    def test_view_ticket_leaves_unredeemed_fields_blank(self, ticket_class) -> None:
        ticket = SimpleNamespace(
            id=ObjectId("64e3b8000000000000000000"),
            code="secret-code",
            issued_to=user_ref("Jane"),
            issued_by=user_ref("issuer-1"),
            purpose="C182 hop-and-hop",
            payment="cash",
            redeemed=None,
            issued_utc=lambda: datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        ticket_class.objects.return_value.first.return_value = ticket

        status, _headers, body = service._view_ticket(
            "64e3b8000000000000000000",
            {"roles": ["admin"]},
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"<dt>Redeemed by</dt><dd></dd>", body)
        self.assertIn(b"<dt>Redemption reason</dt><dd></dd>", body)
        self.assertNotIn(b"unknown", body)

    @patch.object(service, "Ticket")
    def test_solo_ticket_view_hides_code_and_pdf_link(self, ticket_class) -> None:
        user_id = ObjectId("507f1f77bcf86cd799439011")
        ticket_class.objects.return_value.first.return_value = SimpleNamespace(
            id=ObjectId("64e3b8000000000000000000"),
            code="secret-code",
            issued_to=user_ref("Jane", object_id=str(user_id)),
            issued_by=user_ref("issuer"),
            purpose="jump",
            payment="cash",
            redeemed=None,
            issued_utc=lambda: datetime(2026, 8, 23, tzinfo=timezone.utc),
        )

        status, _headers, body = service._view_ticket(
            "64e3b8000000000000000000",
            {"id": user_id, "roles": ["solo"]},
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertNotIn(b"secret-code", body)
        self.assertNotIn(b"<dt>Code</dt>", body)
        self.assertNotIn(b'href="/print?', body)

    @patch.object(service, "Ticket")
    def test_solo_user_cannot_view_another_users_ticket(self, ticket_class) -> None:
        ticket_class.objects.return_value.first.return_value = SimpleNamespace(
            issued_to=user_ref("Other", object_id="507f1f77bcf86cd799439099"),
        )

        status, _headers, body = service._view_ticket(
            "64e3b8000000000000000000",
            {"id": ObjectId("507f1f77bcf86cd799439011"), "roles": ["solo"]},
        )

        self.assertEqual(status, service.HTTPStatus.FORBIDDEN)
        self.assertIn(b"Permission denied.", body)


class ServiceApiTest(unittest.TestCase):
    def request(self, path: str, method: str = "GET", body: bytes = b""):
        return api_module.dispatch(
            {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
                "HTTP_AUTHORIZATION": "******",
            }
        )

    @patch.object(api_report_module, "day_boundaries")
    @patch.object(api_report_module, "User")
    @patch.object(api_report_module, "Ticket")
    @patch.object(api_module, "_verify")
    def test_ticket_redeem_today_report_groups_tickets_by_user(
        self, verify, ticket_class, user_class, day_boundaries
    ) -> None:
        partner_id = ObjectId("507f1f77bcf86cd799439031")
        user_id = ObjectId("507f1f77bcf86cd799439011")
        day_boundaries.return_value = (
            datetime(2026, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 8, 24, tzinfo=timezone.utc),
            datetime(2026, 8, 25, tzinfo=timezone.utc),
        )
        verify.return_value = (SimpleNamespace(id=partner_id), {})
        ticket_class.objects.return_value.only.return_value.order_by.return_value = [
            SimpleNamespace(
                id=ObjectId("64e3b8000000000000000000"),
                issued_to=user_ref("Jane", object_id=str(user_id)),
                payment="cash",
                purpose="jump",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 24, 9, 1, tzinfo=timezone.utc),
                    by=user_ref("redeemer-1", object_id="507f1f77bcf86cd7994390aa"),
                    reason="hop",
                ),
            ),
            SimpleNamespace(
                id=ObjectId("64e3b8000000000000000001"),
                issued_to=user_ref("Jane", object_id=str(user_id)),
                payment="card",
                purpose="tandem",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 24, 10, 2, tzinfo=timezone.utc),
                    by=user_ref("redeemer-2", object_id="507f1f77bcf86cd7994390ab"),
                    reason="done",
                ),
            ),
        ]
        user_class.objects.return_value.only.return_value = [
            SimpleNamespace(id=user_id, display_name="Jane Admin", partner_uid_map={str(partner_id): "ext-jane"})
        ]

        status, _headers, body = self.request("/api/report/ticket-redeem/today")

        self.assertEqual(status, service.HTTPStatus.OK)
        ticket_class.objects.assert_called_once_with(
            redeemed__dt__gte=datetime(2026, 8, 24, tzinfo=timezone.utc),
            redeemed__dt__lt=datetime(2026, 8, 25, tzinfo=timezone.utc),
        )
        user_class.objects.assert_called_once_with(id__in={user_id})
        payload = json.loads(body)
        self.assertEqual(
            payload,
            [
                {
                    "internal_id": str(user_id),
                    "external_id": "ext-jane",
                    "display_name": "Jane Admin",
                    "tickets": [
                        {
                            "internal_id": "64e3b8000000000000000000",
                            "payment": "cash",
                            "purpose": "jump",
                            "redeemed": {
                                "at": "2026-08-24T09:01:00+00:00",
                                "by": {
                                    "internal_id": "507f1f77bcf86cd7994390aa",
                                    "display_name": "redeemer-1",
                                },
                                "reason": "hop",
                            },
                        },
                        {
                            "internal_id": "64e3b8000000000000000001",
                            "payment": "card",
                            "purpose": "tandem",
                            "redeemed": {
                                "at": "2026-08-24T10:02:00+00:00",
                                "by": {
                                    "internal_id": "507f1f77bcf86cd7994390ab",
                                    "display_name": "redeemer-2",
                                },
                                "reason": "done",
                            },
                        },
                    ],
                }
            ],
        )

    @patch.object(api_report_module, "day_boundaries")
    @patch.object(api_report_module, "Ticket")
    @patch.object(api_module, "_verify")
    def test_ticket_redeem_yesterday_report_uses_previous_day_window(self, verify, ticket_class, day_boundaries) -> None:
        day_boundaries.return_value = (
            datetime(2026, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 8, 24, tzinfo=timezone.utc),
            datetime(2026, 8, 25, tzinfo=timezone.utc),
        )
        verify.return_value = (SimpleNamespace(id=ObjectId("507f1f77bcf86cd799439031")), {})
        ticket_class.objects.return_value.only.return_value.order_by.return_value = []

        status, _headers, body = self.request("/api/report/ticket-redeem/yesterday")

        self.assertEqual(status, service.HTTPStatus.OK)
        ticket_class.objects.assert_called_once_with(
            redeemed__dt__gte=datetime(2026, 8, 23, tzinfo=timezone.utc),
            redeemed__dt__lt=datetime(2026, 8, 24, tzinfo=timezone.utc),
        )
        self.assertEqual(json.loads(body), [])

    @patch.object(api_report_module, "day_boundaries")
    @patch.object(api_report_module, "User")
    @patch.object(api_report_module, "Ticket")
    @patch.object(api_module, "_verify")
    def test_ticket_redeem_report_includes_anonymous_ticket_owner_without_user_id(
        self, verify, ticket_class, user_class, day_boundaries
    ) -> None:
        day_boundaries.return_value = (
            datetime(2026, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 8, 24, tzinfo=timezone.utc),
            datetime(2026, 8, 25, tzinfo=timezone.utc),
        )
        verify.return_value = (SimpleNamespace(id=ObjectId("507f1f77bcf86cd799439031")), {})
        ticket_class.objects.return_value.only.return_value.order_by.return_value = [
            SimpleNamespace(
                id=ObjectId("64e3b8000000000000000000"),
                issued_to=user_ref("Walk-in passenger"),
                payment="cash",
                purpose="jump",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 24, 9, 1, tzinfo=timezone.utc),
                    by=user_ref("redeemer-1", object_id="507f1f77bcf86cd7994390aa"),
                    reason="hop",
                ),
            )
        ]
        user_class.objects.return_value.only.return_value = []

        status, _headers, body = self.request("/api/report/ticket-redeem/today")

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertEqual(
            json.loads(body),
            [
                {
                    "internal_id": None,
                    "external_id": None,
                    "display_name": "Walk-in passenger",
                    "tickets": [
                        {
                            "internal_id": "64e3b8000000000000000000",
                            "payment": "cash",
                            "purpose": "jump",
                            "redeemed": {
                                "at": "2026-08-24T09:01:00+00:00",
                                "by": {
                                    "internal_id": "507f1f77bcf86cd7994390aa",
                                    "display_name": "redeemer-1",
                                },
                                "reason": "hop",
                            },
                        }
                    ],
                }
            ],
        )

    @patch.object(api_module, "_verify")
    def test_ticket_redeem_report_allows_only_get(self, verify) -> None:
        verify.return_value = (SimpleNamespace(id=ObjectId("507f1f77bcf86cd799439031")), {})

        status, headers, body = self.request("/api/report/ticket-redeem/today", method="POST")

        self.assertEqual(status, service.HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertIn(("Allow", "GET"), headers)
        self.assertEqual(json.loads(body), {"error": "Method not allowed."})

    @patch.object(api_report_module, "day_boundaries")
    @patch.object(api_report_module, "User")
    @patch.object(api_report_module, "Ticket")
    @patch.object(api_module, "_verify")
    def test_ticket_redeem_report_does_not_merge_anonymous_tickets_without_name(
        self, verify, ticket_class, user_class, day_boundaries
    ) -> None:
        day_boundaries.return_value = (
            datetime(2026, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 8, 24, tzinfo=timezone.utc),
            datetime(2026, 8, 25, tzinfo=timezone.utc),
        )
        verify.return_value = (SimpleNamespace(id=ObjectId("507f1f77bcf86cd799439031")), {})
        ticket_class.objects.return_value.only.return_value.order_by.return_value = [
            SimpleNamespace(
                id=ObjectId("64e3b8000000000000000000"),
                issued_to=SimpleNamespace(id=None, display_name=None),
                payment="cash",
                purpose="jump",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 24, 9, 1, tzinfo=timezone.utc),
                    by=user_ref("redeemer-1", object_id="507f1f77bcf86cd7994390aa"),
                    reason="hop",
                ),
            ),
            SimpleNamespace(
                id=ObjectId("64e3b8000000000000000001"),
                issued_to=SimpleNamespace(id=None, display_name=None),
                payment="card",
                purpose="tandem",
                redeemed=Redemption(
                    dt=datetime(2026, 8, 24, 10, 2, tzinfo=timezone.utc),
                    by=user_ref("redeemer-2", object_id="507f1f77bcf86cd7994390ab"),
                    reason="done",
                ),
            ),
        ]
        user_class.objects.return_value.only.return_value = []

        status, _headers, body = self.request("/api/report/ticket-redeem/today")

        self.assertEqual(status, service.HTTPStatus.OK)
        payload = json.loads(body)
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["tickets"][0]["internal_id"], "64e3b8000000000000000000")
        self.assertEqual(payload[1]["tickets"][0]["internal_id"], "64e3b8000000000000000001")

    @patch.object(api_ticket_module, "User")
    @patch.object(api_ticket_module, "Ticket")
    @patch.object(api_module, "_verify")
    def test_ticket_redeem_api_redeems_ticket_with_display_name_override(self, verify, ticket_class, user_class) -> None:
        partner_id = ObjectId("507f1f77bcf86cd799439031")
        user_id = ObjectId("507f1f77bcf86cd799439011")
        ticket_id = ObjectId("64e3b8000000000000000000")
        verify.return_value = (SimpleNamespace(id=partner_id), {"code": "secret-code", "external_id": "ext-jane"})
        existing = SimpleNamespace(id=ticket_id, code="secret-code")
        updated = SimpleNamespace(
            id=ticket_id,
            code="secret-code",
            redeemed=Redemption(
                dt=datetime(2026, 8, 24, 9, 1, tzinfo=timezone.utc),
                by=user_ref("Name from request", object_id=str(user_id)),
                reason="jump",
            ),
        )
        find_query = SimpleNamespace(first=MagicMock(return_value=existing))
        redeem_query = SimpleNamespace(modify=MagicMock(return_value=updated))
        ticket_class.objects.side_effect = [find_query, redeem_query]
        user_class.objects.return_value.first.return_value = SimpleNamespace(id=user_id, display_name="Name from user")

        status, _headers, body = self.request(
            "/api/ticket/redeem",
            method="POST",
            body=json.dumps(
                {
                    "code": "secret-code",
                    "external_id": "ext-jane",
                    "display_name": "Name from request",
                    "reason": "jump",
                }
            ).encode(),
        )

        self.assertEqual(status, service.HTTPStatus.CREATED)
        ticket_class.objects.assert_has_calls([call(code="secret-code"), call(id=ticket_id, redeemed=None)])
        user_class.objects.assert_called_once_with(**{f"partner_uid_map__{partner_id}": "ext-jane"})
        redeemed_value = redeem_query.modify.call_args.kwargs["set__redeemed"]
        self.assertEqual(redeemed_value.by.id, user_id)
        self.assertEqual(redeemed_value.by.display_name, "Name from request")
        self.assertEqual(redeemed_value.reason, "jump")
        self.assertEqual(json.loads(body), {
            "code": "secret-code",
            "internal_id": "64e3b8000000000000000000",
            "redeemed": {
                "at": "2026-08-24T09:01:00+00:00",
                "by": {
                    "internal_id": "507f1f77bcf86cd799439011",
                    "display_name": "Name from request",
                },
                "reason": "jump",
            },
        })

    @patch.object(api_ticket_module, "Ticket")
    @patch.object(api_module, "_verify")
    def test_ticket_redeem_api_returns_not_found(self, verify, ticket_class) -> None:
        verify.return_value = (SimpleNamespace(id=ObjectId("507f1f77bcf86cd799439031")), {"code": "missing", "external_id": "ext"})
        ticket_class.objects.return_value.first.return_value = None

        status, _headers, body = self.request("/api/ticket/redeem", method="POST")

        self.assertEqual(status, service.HTTPStatus.NOT_FOUND)
        ticket_class.objects.assert_called_once_with(code="missing")
        self.assertEqual(json.loads(body), {"error": "Ticket not found."})

    @patch.object(api_ticket_module, "User")
    @patch.object(api_ticket_module, "Ticket")
    @patch.object(api_module, "_verify")
    def test_ticket_redeem_api_returns_conflict_when_already_redeemed(self, verify, ticket_class, user_class) -> None:
        partner_id = ObjectId("507f1f77bcf86cd799439031")
        verify.return_value = (SimpleNamespace(id=partner_id), {"code": "used", "external_id": "ext"})
        existing = SimpleNamespace(id=ObjectId("64e3b8000000000000000000"))
        find_query = SimpleNamespace(first=MagicMock(return_value=existing))
        redeem_query = SimpleNamespace(modify=MagicMock(return_value=None))
        ticket_class.objects.side_effect = [find_query, redeem_query]
        user_class.objects.return_value.first.return_value = None

        status, _headers, body = self.request("/api/ticket/redeem", method="POST")

        self.assertEqual(status, service.HTTPStatus.CONFLICT)
        ticket_class.objects.assert_has_calls([call(code="used"), call(id=existing.id, redeemed=None)])
        self.assertEqual(json.loads(body), {"error": "Ticket already redeemed."})

    @patch.object(api_module, "_verify")
    def test_ticket_redeem_api_allows_only_post(self, verify) -> None:
        verify.return_value = (SimpleNamespace(id=ObjectId("507f1f77bcf86cd799439031")), {})

        status, headers, body = self.request("/api/ticket/redeem", method="GET")

        self.assertEqual(status, service.HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertIn(("Allow", "POST"), headers)
        self.assertEqual(json.loads(body), {"error": "Method not allowed."})

    @patch.object(api_module, "_verify")
    def test_ticket_redeem_api_validates_required_fields(self, verify) -> None:
        verify.return_value = (SimpleNamespace(id=ObjectId("507f1f77bcf86cd799439031")), {"code": "secret-code"})

        status, _headers, body = self.request("/api/ticket/redeem", method="POST")

        self.assertEqual(status, service.HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(body), {"error": "code and external_id are required."})


class ApiTimestampTest(unittest.TestCase):
    now = datetime(2026, 8, 30, 18, 45, 0, tzinfo=timezone.utc)

    def test_dt_claim_accepts_timestamp_within_one_minute(self) -> None:
        api_shared._validate_dt("20260830T184559Z", now=self.now)

    def test_dt_claim_rejects_missing_or_malformed_timestamp(self) -> None:
        for value in (None, "2026-08-30T18:45:00Z", "20260830T1845Z"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(PermissionError, "required|invalid"):
                    api_shared._validate_dt(value, now=self.now)

    def test_dt_claim_rejects_timestamp_outside_one_minute(self) -> None:
        with self.assertRaisesRegex(PermissionError, "outside"):
            api_shared._validate_dt("20260830T184601Z", now=self.now)

    def test_dt_claim_accepts_exact_one_minute_boundary(self) -> None:
        api_shared._validate_dt("20260830T184600Z", now=self.now)


class ServiceApplicationTest(unittest.TestCase):
    def request(
        self,
        path: str,
        method: str = "GET",
        form: Optional[dict] = None,
        authenticated: bool = True,
        roles: Optional[list[str]] = None,
    ):
        roles = roles if roles is not None else ["admin"]
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
            "_session_user",
            return_value=SimpleNamespace(
                id=ObjectId("507f1f77bcf86cd799439011"),
                display_name="Jane",
                roles=roles,
            )
            if authenticated
            else None,
        ), patch.object(
            service._auth_module,
            "current_user_ref",
            return_value={
                "id": ObjectId("507f1f77bcf86cd799439011"),
                "display_name": "Jane",
                "roles": roles,
            }
            if authenticated
            else None,
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

    def test_redeem_page_offers_the_com_port_scanner(self) -> None:
        response = self.request("/redeem")

        self.assertEqual(response["status"], "200 OK")
        self.assertIn(b"Use COM port scanner", response["body"])
        self.assertIn(b"navigator.serial", response["body"])

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

    def test_admin_navigation_is_only_shown_to_administrators(self) -> None:
        with patch.object(service._auth_module, "current_user_roles", return_value=["admin"]):
            admin_response = self.request("/")
        user_response = self.request("/", roles=["solo"])

        self.assertIn(b'href="/admin">Admin</a>', admin_response["body"])
        self.assertNotIn(b'href="/admin/user/new"', admin_response["body"])
        self.assertNotIn(b'href="/admin">Admin</a>', user_response["body"])

    def test_admin_page_lists_admin_functions(self) -> None:
        response = self.request("/admin")

        self.assertEqual(response["status"], "200 OK")
        self.assertIn(b'href="/admin/user/list">Users</a>', response["body"])
        self.assertIn(b'href="/admin/partner/list">Partners</a>', response["body"])
        self.assertNotIn(b'href="/admin/user/new">Add user</a>', response["body"])

    def test_admin_user_list_route_uses_handler(self) -> None:
        with patch.object(service, "_list_users", return_value=(service.HTTPStatus.OK, [], b"users")) as handler:
            response = self.request("/admin/user/list")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["body"], b"users")
        handler.assert_called_once_with()

    def test_admin_user_update_route_uses_handler(self) -> None:
        user_id = "507f1f77bcf86cd799439011"
        form = {"action": "update", "name": "Jane", "email": "", "role": "solo"}
        with patch.object(service, "_update_user", return_value=(service.HTTPStatus.SEE_OTHER, [], b"")) as handler:
            response = self.request(f"/admin/user/view/{user_id}", "POST", form)

        self.assertEqual(response["status"], "303 See Other")
        handler.assert_called_once_with(user_id, form)

    def test_admin_new_user_route_requires_admin_role(self) -> None:
        with patch.object(
            service._auth_module,
            "require_role",
            return_value=(service.HTTPStatus.FORBIDDEN, [], b"denied"),
        ) as require_role:
            response = self.request("/admin/user/new")

        self.assertEqual(response["status"], "403 Forbidden")
        require_role.assert_called_once_with(ANY, "admin")

    def test_admin_new_user_route_renders_form_for_admin(self) -> None:
        with patch.object(service._auth_module, "require_role", return_value=None):
            response = self.request("/admin/user/new")

        self.assertEqual(response["status"], "200 OK")
        self.assertIn(b'name="identity_type"', response["body"])
        self.assertIn(b'name="role"', response["body"])

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

    def test_ticket_detail_route_uses_handler(self) -> None:
        with patch.object(service, "_view_ticket", return_value=(service.HTTPStatus.OK, [], b"ticket")) as handler:
            response = self.request("/ticket/64e3b8000000000000000000")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["body"], b"ticket")
        handler.assert_called_once_with("64e3b8000000000000000000", ANY)

    def test_ticket_detail_route_accepts_a_trailing_slash(self) -> None:
        with patch.object(service, "_view_ticket", return_value=(service.HTTPStatus.OK, [], b"ticket")) as handler:
            response = self.request("/ticket/64e3b8000000000000000000/")

        self.assertEqual(response["status"], "200 OK")
        handler.assert_called_once_with("64e3b8000000000000000000", ANY)

    def test_ticket_detail_route_requires_an_object_id_path(self) -> None:
        with patch.object(service, "_view_ticket") as handler:
            response = self.request("/ticket/not-an-id")

        self.assertEqual(response["status"], "404 Not Found")
        handler.assert_not_called()

    def test_protected_routes_redirect_to_authentication(self) -> None:
        response = self.request("/issue", authenticated=False)

        self.assertEqual(response["status"], "303 See Other")
        self.assertEqual(response["headers"]["Location"], "/authn?return_uri=%2Fissue")

    def test_solo_user_only_lists_their_own_tickets(self) -> None:
        with patch.object(service, "_view_owner_tickets", return_value=(service.HTTPStatus.OK, [], b"own")) as handler:
            response = self.request(
                "/tickets",
                roles=["solo"],
            )

        self.assertEqual(response["status"], "200 OK")
        handler.assert_called_once_with("507f1f77bcf86cd799439011", None)

    def test_solo_user_cannot_issue_tickets(self) -> None:
        response = self.request("/issue", roles=["solo"])

        self.assertEqual(response["status"], "403 Forbidden")

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

        with patch.object(fido2_module, "server", return_value=server), patch.object(
            auth, "User", user_class
        ), patch.object(fido2_module, "credential_data", return_value="credential data"):
            _status, headers, body = auth.begin_authn(environ)

        server.authenticate_begin.assert_called_once_with(["credential data"], challenge=ANY)
        cookie = next(value for name, value in headers if name == "Set-Cookie")
        payload = auth._unsign(cookie.split(";", 1)[0].split("=", 1)[1])
        self.assertEqual(payload["state"], state)
        self.assertIn("Path=/authn", cookie)
        self.assertIn(b"navigator.credentials.get", body)
        self.assertIn(b"registrationOptions = null", body)
        self.assertNotIn(b"Register credential", body)

    def test_authn_begin_remembers_the_requested_return_uri(self) -> None:
        auth = service._auth_module
        server = MagicMock()
        server.authenticate_begin.return_value = {}, {"challenge": b64(b"challenge")}
        user_class = MagicMock()
        user_class.objects.return_value.only.return_value = []
        environ = {
            "PATH_INFO": "/authn",
            "QUERY_STRING": "return_uri=%2Ftickets%3Fuser_id%3D123",
            "HTTP_HOST": "example.test",
            "wsgi.url_scheme": "https",
        }

        with patch.object(fido2_module, "server", return_value=server), patch.object(auth, "User", user_class):
            _status, headers, _body = auth.begin_authn(environ)

        cookie = next(value for name, value in headers if name == "Set-Cookie" and "authn_challenge=" in value)
        payload = auth._unsign(cookie.split(";", 1)[0].split("=", 1)[1])
        self.assertEqual(payload["return_uri"], "/tickets?user_id=123")

    def test_fido2_server_requests_enterprise_attestation(self) -> None:
        auth = service._auth_module
        environ = {
            "HTTP_HOST": "example.test",
            "wsgi.url_scheme": "https",
        }

        server = fido2_module.server(environ)

        self.assertEqual(server.attestation, AttestationConveyancePreference.ENTERPRISE)

    def test_authn_complete_uses_stored_credential_and_sets_session_cookie(self) -> None:
        auth = service._auth_module
        challenge = b"challenge"
        cookie = "authn_challenge=" + auth._signed(
            {"challenge": b64(challenge), "issued": auth.time(), "return_uri": "/issue"}
        )
        server = MagicMock()
        credential = SimpleNamespace(id=b"credential")

        with patch.object(fido2_module, "server", return_value=server), patch.object(
            auth, "_find_credential", return_value=credential
        ), patch.object(fido2_module, "credential_data", return_value="credential data"):
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
        self.assertEqual(response["headers"]["Location"], "/issue")
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
            fido2_module, "server", return_value=server
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
            fido2_module, "server", return_value=server
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
        user_class.assert_called_once_with(
            id=ObjectId(user_id),
            display_name="Jane Sky",
            roles=["admin"],
        )
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
            email=None,
            email_authentication=None,
            google_credentials=[],
            microsoft_credentials=[],
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

        with patch.object(fido2_module, "server", return_value=server), patch.object(
            auth, "User", MagicMock(objects=MagicMock(return_value=MagicMock(only=MagicMock(return_value=[]))))
        ), patch.object(auth, "_session_user", return_value=user), patch.object(
            fido2_module, "credential_data", return_value="credential data"
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
            "PATH_INFO": "/authn/fido2/add",
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
        ), patch.object(fido2_module, "server", return_value=server), patch.object(
            auth, "_find_credential", return_value=None
        ), patch.object(fido2_module, "CollectedClientData", return_value="client data"), patch.object(
            fido2_module, "AttestationObject", return_value="attestation"
        ), patch.object(
            fido2_module, "AuthenticatorAttestationResponse", return_value="attestation response"
        ), patch.object(fido2_module, "RegistrationResponse", return_value="registration response"):
            response = fido2_module.add_credential(environ)

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
            "HTTP_COOKIE": "authn_csrf=token",
        }

        with patch.object(auth, "_session_user", return_value=user):
            status, headers, _body = fido2_module.remove_credential(environ)

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

    def test_authenticated_page_orders_the_credential_sections(self) -> None:
        auth = service._auth_module
        server = MagicMock()
        server.authenticate_begin.return_value = {}, {"challenge": b64(b"challenge"), "user_verification": None}
        server.register_begin.return_value = (
            {"publicKey": {"challenge": b"register challenge"}},
            {"challenge": b64(b"register challenge"), "user_verification": "discouraged"},
        )
        user = SimpleNamespace(
            id=ObjectId("507f1f77bcf86cd799439011"),
            display_name="Jane",
            email=None,
            email_authentication=None,
            fido2_credentials=[],
            google_credentials=[SimpleNamespace(email="jane@gmail.test")],
            microsoft_credentials=[SimpleNamespace(email="jane@outlook.test")],
        )
        environ = {"PATH_INFO": "/authn", "HTTP_HOST": "example.test", "wsgi.url_scheme": "https"}

        with patch.object(fido2_module, "server", return_value=server), patch.object(
            auth, "User", MagicMock(objects=MagicMock(return_value=MagicMock(only=MagicMock(return_value=[]))))
        ), patch.object(auth, "_session_user", return_value=user), patch.object(
            fido2_module, "credential_data", return_value="credential data"
        ):
            _status, _headers, body = auth.begin_authn(environ)

        self.assertLess(body.index(b"My FIDO2 credentials"), body.index(b"My Google credentials"))
        self.assertLess(body.index(b"My Google credentials"), body.index(b"My Microsoft credentials"))

    def test_unauthenticated_page_keeps_the_sign_in_section_order(self) -> None:
        auth = service._auth_module
        server = MagicMock()
        server.authenticate_begin.return_value = {}, {"challenge": b64(b"challenge"), "user_verification": None}
        environ = {"PATH_INFO": "/authn", "HTTP_HOST": "example.test", "wsgi.url_scheme": "https"}

        with patch.object(fido2_module, "server", return_value=server), patch.object(
            auth, "User", MagicMock(objects=MagicMock(return_value=MagicMock(only=MagicMock(return_value=[]))))
        ), patch.object(auth, "_session_user", return_value=None):
            _status, _headers, body = auth.begin_authn(environ)

        self.assertNotIn(b"My FIDO2 credentials", body)
        self.assertLess(body.index(b"<h2>FIDO2/passkey</h2>"), body.index(b"<h2>Google</h2>"))
        self.assertLess(body.index(b"<h2>Google</h2>"), body.index(b"<h2>Microsoft</h2>"))



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


class ServiceMicrosoftTest(unittest.TestCase):
    def environ(self, query: str = "", cookie: str = "") -> dict:
        return {
            "PATH_INFO": "/authn/microsoft",
            "QUERY_STRING": query,
            "REQUEST_METHOD": "GET",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
            "HTTP_HOST": "example.test",
            "HTTP_COOKIE": cookie,
            "wsgi.url_scheme": "https",
        }

    def certificate_pem(self, key=None) -> tuple[str, "x509.Certificate"]:
        key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.test")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime(2026, 1, 1))
            .not_valid_after(datetime(2027, 1, 1))
            .sign(key, None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256())
        )
        pem = certificate.public_bytes(serialization.Encoding.PEM).decode() + key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        return pem, certificate

    @staticmethod
    def segment(value: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

    @staticmethod
    def signature(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def test_begin_requests_the_openid_and_email_scopes(self) -> None:
        with patch.object(microsoft_module, "_configured", return_value=True), patch.object(
            microsoft_module, "_session_user", return_value=None
        ), patch.object(
            microsoft_module,
            "_endpoints",
            return_value=("https://login.test/auth", "https://login.test/token", "https://login.test/me"),
        ), patch.object(microsoft_module, "microsoft_client_id", return_value="client"):
            status, headers, _body = microsoft_module.begin(self.environ())

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        location = dict(headers)["Location"]
        scope = parse_qs(location.split("?", 1)[1])["scope"][0]
        self.assertEqual(scope.split(), ["openid", "email"])

    def test_the_certificate_takes_precedence_over_the_secret(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem, certificate = self.certificate_pem(key)
        with patch.object(microsoft_module, "microsoft_client_certificate", return_value=pem), patch.object(
            microsoft_module, "microsoft_client_secret", return_value="shhh"
        ), patch.object(microsoft_module, "microsoft_client_id", return_value="client"):
            authentication = microsoft_module._client_authentication("https://login.test/token")

        self.assertNotIn("client_secret", authentication)
        self.assertEqual(
            authentication["client_assertion_type"],
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        )
        header_segment, claims_segment, signature_segment = authentication["client_assertion"].split(".")
        expected_thumbprint = base64.urlsafe_b64encode(
            certificate.fingerprint(hashes.SHA256())
        ).rstrip(b"=").decode()
        self.assertEqual(
            self.segment(header_segment), {"alg": "RS256", "typ": "JWT", "x5t#S256": expected_thumbprint}
        )
        claims = self.segment(claims_segment)
        self.assertEqual(claims["aud"], "https://login.test/token")
        self.assertEqual(claims["iss"], "client")
        self.assertEqual(claims["sub"], "client")
        self.assertGreater(claims["exp"], claims["iat"])
        key.public_key().verify(
            self.signature(signature_segment),
            f"{header_segment}.{claims_segment}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_an_elliptic_curve_key_uses_microsoft_es256_with_sha256(self) -> None:
        key = ec.generate_private_key(ec.SECP384R1())
        pem, _certificate = self.certificate_pem(key)
        with patch.object(microsoft_module, "microsoft_client_certificate", return_value=pem), patch.object(
            microsoft_module, "microsoft_client_id", return_value="client"
        ):
            assertion = microsoft_module._client_assertion("https://login.test/token")

        header_segment, claims_segment, signature_segment = assertion.split(".")
        self.assertEqual(self.segment(header_segment)["alg"], "ES256")
        raw = self.signature(signature_segment)
        self.assertEqual(len(raw), 96)
        half = len(raw) // 2
        key.public_key().verify(
            encode_dss_signature(
                int.from_bytes(raw[:half], "big"), int.from_bytes(raw[half:], "big")
            ),
            f"{header_segment}.{claims_segment}".encode(),
            ec.ECDSA(hashes.SHA256()),
        )

    def test_the_certificate_is_read_when_the_key_comes_first_in_the_bundle(self) -> None:
        pem, certificate = self.certificate_pem()
        certificate_pem, key_pem = pem.split("-----BEGIN PRIVATE KEY-----", 1)
        reversed_pem = "-----BEGIN PRIVATE KEY-----" + key_pem + certificate_pem
        with patch.object(microsoft_module, "microsoft_client_certificate", return_value=reversed_pem), patch.object(
            microsoft_module, "microsoft_client_id", return_value="client"
        ):
            assertion = microsoft_module._client_assertion("https://login.test/token")

        expected_thumbprint = base64.urlsafe_b64encode(
            certificate.fingerprint(hashes.SHA256())
        ).rstrip(b"=").decode()
        self.assertEqual(self.segment(assertion.split(".", 1)[0])["x5t#S256"], expected_thumbprint)

    def test_the_secret_is_used_without_a_certificate(self) -> None:
        with patch.object(microsoft_module, "microsoft_client_certificate", return_value=""), patch.object(
            microsoft_module, "microsoft_client_secret", return_value="shhh"
        ):
            self.assertEqual(
                microsoft_module._client_authentication("https://login.test/token"), {"client_secret": "shhh"}
            )

    def test_an_invalid_certificate_is_reported_without_the_key_material(self) -> None:
        with patch.object(
            microsoft_module, "microsoft_client_certificate", return_value="-----BEGIN PRIVATE KEY-----\nsecret\n"
        ):
            with self.assertRaises(ValueError) as raised:
                microsoft_module._client_authentication("https://login.test/token")

        self.assertEqual(str(raised.exception), "Microsoft certificate is invalid.")
        report = "".join(traceback.format_exception(type(raised.exception), raised.exception, raised.exception.__traceback__))
        self.assertNotIn("secret", report)

    def test_an_encrypted_private_key_is_reported_as_an_invalid_certificate(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"password"),
        ).decode()
        with patch.object(microsoft_module, "microsoft_client_certificate", return_value=pem):
            with self.assertRaises(ValueError) as raised:
                microsoft_module._client_authentication("https://login.test/token")

        self.assertEqual(str(raised.exception), "Microsoft certificate is invalid.")
        self.assertIsInstance(raised.exception.__cause__, (TypeError, ValueError))

    def test_a_certificate_alone_configures_microsoft_authentication(self) -> None:
        with patch.object(microsoft_module, "microsoft_client_id", return_value="client"), patch.object(
            microsoft_module, "microsoft_client_certificate", return_value="pem"
        ), patch.object(microsoft_module, "microsoft_client_secret", return_value=""):
            self.assertTrue(microsoft_module._configured())

    def test_complete_authenticates_the_token_request_with_the_client_assertion(self) -> None:
        token_response = MagicMock()
        token_response.json.return_value = {"access_token": "token"}
        profile_response = MagicMock()
        profile_response.json.return_value = {"email": "Jane@Example.test"}
        user = MagicMock(id="Jane")
        with patch.object(
            microsoft_module, "_state", return_value={"state": "abc", "user": None, "code_verifier": "verifier"}
        ), patch.object(
            microsoft_module,
            "_endpoints",
            return_value=("https://login.test/auth", "https://login.test/token", "https://login.test/me"),
        ), patch.object(microsoft_module, "microsoft_client_id", return_value="client"), patch.object(
            microsoft_module, "_client_authentication", return_value={"client_assertion": "jwt"}
        ), patch.object(microsoft_module.requests, "post", return_value=token_response) as post, patch.object(
            microsoft_module.requests, "get", return_value=profile_response
        ), patch.object(microsoft_module, "_session_user", return_value=None), patch.object(
            microsoft_module, "User"
        ) as user_class:
            user_class.objects.return_value.first.return_value = user
            status, _headers, _body = microsoft_module.complete(self.environ(query="state=abc&code=auth-code"))

        self.assertEqual(status, service.HTTPStatus.SEE_OTHER)
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["client_assertion"], "jwt")
        self.assertNotIn("client_secret", data)
        self.assertEqual(data["scope"], "openid email")


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

    def test_timezone_defaults_to_utc(self) -> None:
        self.patch_config({})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.local_timezone().key, "UTC")

    def test_timezone_is_read_from_the_yaml_file(self) -> None:
        self.patch_config({"timezone": "America/Los_Angeles"})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.local_timezone().key, "America/Los_Angeles")

    def test_business_name_defaults_to_the_dropzone(self) -> None:
        self.patch_config({})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.business_name(), "The Dropzone")

    def test_business_name_is_read_from_the_yaml_file(self) -> None:
        self.patch_config({"business_name": "Skydive Example"})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.business_name(), "Skydive Example")

    def test_google_settings_are_read_from_the_google_section(self) -> None:
        self.patch_config({"google": {"client_id": "client", "secret": "shhh"}})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.google_client_id(), "client")
            self.assertEqual(config.google_client_secret(), "shhh")

    def test_microsoft_settings_are_read_from_the_microsoft_section(self) -> None:
        self.patch_config(
            {"microsoft": {"client_id": "client", "secret": "shhh", "certificate": "-----BEGIN CERTIFICATE-----"}}
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.microsoft_client_id(), "client")
            self.assertEqual(config.microsoft_client_secret(), "shhh")
            self.assertEqual(config.microsoft_client_certificate(), "-----BEGIN CERTIFICATE-----")

    def test_a_missing_microsoft_certificate_is_empty(self) -> None:
        self.patch_config({"microsoft": {"client_id": "client", "secret": "shhh"}})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.microsoft_client_certificate(), "")

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
