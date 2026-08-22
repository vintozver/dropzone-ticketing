from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from io import BytesIO

from dropzone_ticketing import PDF, Ticket
from dropzone_ticketing.pdf import PAGE_HEIGHT, PAGE_WIDTH, load_logo_bytes


class TicketPdfTest(unittest.TestCase):
    def test_ticket_produces_single_page_pdf_with_expected_size_and_text(self) -> None:
        output = BytesIO()
        ticket = Ticket(
            identifier="ticket-1",
            code="ABC123",
            date_issued=datetime(2026, 8, 21, 20, 30, 0, tzinfo=timezone.utc),
            owner="Jane Jumper",
        )

        pdf = PDF(output)
        pdf.append(ticket)
        pdf.render()

        rendered = output.getvalue().decode("latin1")
        self.assertTrue(rendered.startswith("%PDF-"))
        self.assertEqual(len(re.findall(r"/Type /Page\b", rendered)), 1)
        self.assertIn(f"/MediaBox [ 0 0 {PAGE_WIDTH:g} {PAGE_HEIGHT:g} ]", rendered)
        self.assertIn("(Jane Jumper) Tj", rendered)
        self.assertIn("(ABC123) Tj", rendered)
        self.assertIn("(2026-08-21 20:30:00 UTC) Tj", rendered)
        self.assertIn("(Issued: 2026-08-21 13:30:00 PDT) Tj", rendered)
        self.assertIn("(Skydive Toledo LLC) Tj", rendered)
        self.assertIn("(One jump 36$) Tj", rendered)
        self.assertIn("(Paid with card xxxx-0000) Tj", rendered)
        self.assertIn("(To: Jane Jumper) Tj", rendered)
        self.assertIn("(Jumper:) Tj", rendered)
        self.assertIn("(_____________________) Tj", rendered)

    def test_naive_date_issued_is_treated_as_utc(self) -> None:
        output = BytesIO()
        ticket = Ticket(
            identifier="ticket-2",
            code="XYZ789",
            date_issued=datetime(2026, 1, 15, 12, 0, 0),
            owner="John Jumper",
        )

        pdf = PDF(output)
        pdf.append(ticket)
        pdf.render()

        rendered = output.getvalue().decode("latin1")
        self.assertIn("(2026-01-15 12:00:00 UTC) Tj", rendered)
        self.assertIn("(Issued: 2026-01-15 05:00:00 PDT) Tj", rendered)


class TicketLogoTest(unittest.TestCase):
    def test_logo_resource_is_available_as_png(self) -> None:
        logo = load_logo_bytes()

        self.assertTrue(logo.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_ticket_pdf_embeds_the_logo_image(self) -> None:
        output = BytesIO()
        ticket = Ticket(
            identifier="ticket-3",
            code="LOGO01",
            date_issued=datetime(2026, 8, 21, 20, 30, 0, tzinfo=timezone.utc),
            owner="Jane Jumper",
        )

        pdf = PDF(output)
        pdf.append(ticket)
        pdf.render()

        rendered = output.getvalue().decode("latin1")
        self.assertIn("/Subtype /Image", rendered)


if __name__ == "__main__":
    unittest.main()
