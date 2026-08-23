from __future__ import annotations

import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlencode

from mongoengine.errors import NotUniqueError

from dropzone_ticketing import service
from dropzone_ticketing.model.ticket import Redemption


def b64(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


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
                "owner": "Jane",
                "count": "1",
                "payment": "cash",
                "purpose": "C182 hop-and-hop",
            }
        )

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertNotIn(b"new-code", body)
        self.assertEqual(generate_code.call_count, 2)
        self.assertEqual(
            ticket_class.call_args_list,
            [
                call(code="duplicate-code", owner="Jane", payment="cash", purpose="C182 hop-and-hop"),
                call(code="new-code", owner="Jane", payment="cash", purpose="C182 hop-and-hop"),
            ],
        )

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
                "owner": "Jane",
                "count": "2",
                "payment": "cash",
                "purpose": "C182 hop-and-hop",
            }
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
        ticket = MagicMock(code="secret-code", owner="Jane")
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
        tickets = [MagicMock(code="code-1", owner="Jane"), MagicMock(code="code-2", owner="Jane")]
        ticket_class.objects.return_value = tickets

        with patch.object(service, "PDF") as pdf_class:
            status, headers, _body = service._print_tickets([], "Jane")

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(("Content-Type", "application/pdf"), headers)
        ticket_class.objects.assert_called_once_with(owner="Jane", redeemed=None)
        self.assertEqual(
            pdf_class.return_value.append.call_args_list,
            [call(tickets[0]), call(tickets[1])],
        )

    def test_print_rejects_an_owner_together_with_ticket_ids(self) -> None:
        status, _headers, body = service._print_tickets(["507f1f77bcf86cd799439011"], "Jane")

        self.assertEqual(status, service.HTTPStatus.BAD_REQUEST)
        self.assertIn(b"Supply either ticket identifiers or an owner, not both.", body)

    def test_print_rejects_a_blank_owner(self) -> None:
        status, _headers, body = service._print_tickets([], " ")

        self.assertEqual(status, service.HTTPStatus.BAD_REQUEST)
        self.assertIn(b"Owner is required.", body)

    @patch.object(service, "Ticket")
    def test_print_without_active_owner_tickets_is_not_found(self, ticket_class) -> None:
        ticket_class.objects.return_value = []

        status, _headers, body = service._print_tickets([], "Jane")

        self.assertEqual(status, service.HTTPStatus.NOT_FOUND)
        self.assertIn(b"No tickets found.", body)

    @patch.object(service, "Ticket")
    def test_view_owners_lists_only_owners_with_active_tickets(self, ticket_class) -> None:
        ticket_class.objects.return_value.distinct.return_value = ["Zoe", "Jane"]

        status, _headers, body = service._view_owners()

        self.assertEqual(status, service.HTTPStatus.OK)
        ticket_class.objects.assert_called_once_with(redeemed=None)
        ticket_class.objects.return_value.distinct.assert_called_once_with("owner")
        self.assertLess(body.index(b"Jane"), body.index(b"Zoe"))

    @patch.object(service, "Ticket")
    def test_view_owner_tickets_hides_the_ticket_code(self, ticket_class) -> None:
        ticket = MagicMock(
            payment="cash",
            purpose="C182 hop-and-hop",
            code="secret-code",
            id="507f1f77bcf86cd799439011",
        )
        ticket.issued_utc.return_value = datetime(2026, 8, 22, tzinfo=timezone.utc)
        ticket_class.objects.return_value = [ticket]

        status, _headers, body = service._view_owner_tickets("Jane")

        self.assertEqual(status, service.HTTPStatus.OK)
        ticket_class.objects.assert_called_once_with(owner="Jane", redeemed=None)
        self.assertIn(b"2026-08-22 00:00:00 UTC", body)
        self.assertIn(b"C182 hop-and-hop", body)
        self.assertIn(b"cash", body)
        self.assertNotIn(b"secret-code", body)
        self.assertIn(b'href="/print?id=507f1f77bcf86cd799439011"', body)
        self.assertIn(b'href="/print?owner=Jane"', body)

    @patch.object(service.mongoengine, "register_connection")
    def test_ensure_storage_registers_connection_from_env_var(self, register_connection) -> None:
        original_connected = service._storage_connected
        service._storage_connected = False
        try:
            with patch.dict("os.environ", {"MONGODB_CONNECTION_STRING": "mongodb://example/test"}):
                service._ensure_storage()

                register_connection.assert_called_once_with(
                    service.mongoengine_alias,
                    host="mongodb://example/test",
                )
                self.assertTrue(service._storage_connected)

                # A second call should be a no-op and must not reconnect.
                service._ensure_storage()
                register_connection.assert_called_once()
        finally:
            service._storage_connected = original_connected


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
        ):
            response["body"] = b"".join(service.application(environ, start_response))
        return response

    def test_index_is_html(self) -> None:
        response = self.request("/")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["headers"]["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Issue tickets", response["body"])

    def test_issue_rejects_out_of_range_count(self) -> None:
        response = self.request(
            "/issue",
            "POST",
            {
                "owner": "Jane",
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
                "owner": "Jane",
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
                "owner": "Jane",
                "count": "1",
                "payment": "cash",
                "purpose": " ",
            },
        )

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertIn(b"Purpose is required.", response["body"])

    def test_print_page_is_removed(self) -> None:
        response = self.request("/print")

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertNotIn(b"Print tickets", response["body"])

    def test_navigation_does_not_link_the_print_page(self) -> None:
        response = self.request("/")

        self.assertNotIn(b'href="/print"', response["body"])

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

    def test_authn_page_requires_yubikey_allowlist_configuration(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            response = self.request("/authn")

        self.assertEqual(response["status"], "503 Service Unavailable")

    def test_authn_sets_a_challenge_cookie_when_configured(self) -> None:
        with patch.dict("os.environ", {"AUTHN_YUBIKEY_IDS": "1234567"}):
            response = self.request("/authn")

        self.assertEqual(response["status"], "200 OK")
        self.assertIn("authn_challenge=", response["headers"]["Set-Cookie"])
        self.assertIn(b"Authenticate with YubiKey", response["body"])

    def test_authn_completes_with_verified_allowed_yubikey(self) -> None:
        challenge = b"challenge"
        cookie = "authn_challenge=" + service._auth_module._signed(
            {"challenge": b64(challenge), "issued": service._auth_module.time()}
        )
        with patch.dict("os.environ", {"AUTHN_YUBIKEY_IDS": "1234567"}), patch.object(
            service._auth_module, "_verify_yubikey_attestation", return_value="1234567"
        ) as verify:
            response = self.request(
                "/authn",
                "POST",
                {"attestationObject": b64(b"attestation"), "clientDataJSON": b64(b"client")},
                cookie,
            )

        self.assertEqual(response["status"], "303 See Other")
        self.assertEqual(response["headers"]["Location"], "/")
        self.assertIn("authn_session=", "\n".join(value for name, value in response["raw_headers"] if name == "Set-Cookie"))
        verify.assert_called_once()

    def test_authn_rejects_unverifiable_attestation(self) -> None:
        challenge = b"challenge"
        cookie = "authn_challenge=" + service._auth_module._signed(
            {"challenge": b64(challenge), "issued": service._auth_module.time()}
        )
        with patch.dict("os.environ", {"AUTHN_YUBIKEY_IDS": "1234567"}), patch.object(
            service._auth_module, "_verify_yubikey_attestation", side_effect=ValueError
        ):
            response = self.request(
                "/authn",
                "POST",
                {"attestationObject": b64(b"attestation"), "clientDataJSON": b64(b"client")},
                cookie,
            )

        self.assertEqual(response["status"], "403 Forbidden")

    def test_verify_rejects_disallowed_yubikey_serial_number(self) -> None:
        verifier = service._auth_module._YubiKeyAttestationVerifier(frozenset({"1234567"}))
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = [SimpleNamespace(value="7654321")]
        cert.subject.__iter__.return_value = iter([SimpleNamespace(value="Yubico")])
        cert.issuer.__iter__.return_value = iter([SimpleNamespace(value="Yubico")])

        with patch.object(service._auth_module.x509, "load_der_x509_certificate", return_value=cert):
            with self.assertRaises(service._auth_module.UntrustedAttestation):
                verifier.ca_lookup(SimpleNamespace(trust_path=[b"cert"]), MagicMock())

    def test_verify_rejects_non_yubikey_attestation_certificate(self) -> None:
        verifier = service._auth_module._YubiKeyAttestationVerifier(frozenset({"1234567"}))
        cert = MagicMock()
        cert.subject.get_attributes_for_oid.return_value = [SimpleNamespace(value="1234567")]
        cert.subject.__iter__.return_value = iter([SimpleNamespace(value="Other")])
        cert.issuer.__iter__.return_value = iter([SimpleNamespace(value="Other")])

        with patch.object(service._auth_module.x509, "load_der_x509_certificate", return_value=cert):
            with self.assertRaises(service._auth_module.UntrustedAttestation):
                verifier.ca_lookup(SimpleNamespace(trust_path=[b"cert"]), MagicMock())


if __name__ == "__main__":
    unittest.main()
