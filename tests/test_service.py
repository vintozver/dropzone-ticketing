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
        saved = MagicMock(code="new-code")
        ticket_class.side_effect = [duplicate, saved]

        status, _headers, body = service._issue({"owner": "Jane", "count": "1"})

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"new-code", body)
        self.assertEqual(generate_code.call_count, 2)
        self.assertEqual(
            ticket_class.call_args_list,
            [call(code="duplicate-code", owner="Jane"), call(code="new-code", owner="Jane")],
        )

    @patch.object(service, "Ticket")
    def test_redeem_reports_each_result(self, ticket_class) -> None:
        redeemed_at = datetime(2026, 8, 22, tzinfo=timezone.utc)
        active = MagicMock(redeemed=None)
        already_redeemed = MagicMock(redeemed=redeemed_at)
        tickets = {
            "active": active,
            "used": already_redeemed,
            "missing": None,
        }
        ticket_class.objects.side_effect = lambda code: SimpleNamespace(
            first=lambda: tickets[code]
        )

        status, _headers, body = service._redeem({"codes": "active used missing"})

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(b"redeemed OK", body)
        self.assertIn(b"already redeemed", body)
        self.assertIn(b"not found", body)
        active.save.assert_called_once_with()
        self.assertIsNotNone(active.redeemed)

    @patch.object(service, "Ticket")
    def test_print_without_active_tickets_returns_html_message(self, ticket_class) -> None:
        ticket_class.objects.return_value = []

        status, headers, body = service._print_tickets("Jane")

        self.assertEqual(status, service.HTTPStatus.OK)
        self.assertIn(("Content-Type", "text/html; charset=utf-8"), headers)
        self.assertIn(b"No active tickets found", body)


class ServiceApplicationTest(unittest.TestCase):
    def request(self, path: str, method: str = "GET", form: Optional[dict] = None):
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

        with patch.object(service, "_ensure_storage"):
            response["body"] = b"".join(service.application(environ, start_response))
        return response

    def test_index_is_html(self) -> None:
        response = self.request("/")

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["headers"]["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Issue tickets", response["body"])

    def test_issue_rejects_out_of_range_count(self) -> None:
        response = self.request("/issue", "POST", {"owner": "Jane", "count": "1001"})

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertIn(b"between 1 and 1000", response["body"])

    def test_unknown_path_is_not_found(self) -> None:
        response = self.request("/missing")

        self.assertEqual(response["status"], "404 Not Found")

    def test_wrong_method_includes_allow_header(self) -> None:
        response = self.request("/redeem", "DELETE")

        self.assertEqual(response["status"], "405 Method Not Allowed")
        self.assertEqual(response["headers"]["Allow"], "GET, POST")


if __name__ == "__main__":
    unittest.main()
