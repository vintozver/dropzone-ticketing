from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from io import BytesIO

from dropzone_ticketing import Ticket
from dropzone_ticketing.ticket import PAGE_HEIGHT, PAGE_WIDTH


class TicketPdfTest(unittest.TestCase):
    def test_ticket_produces_single_page_pdf_with_expected_size_and_text(self) -> None:
        output = BytesIO()
        ticket = Ticket(
            identifier="ticket-1",
            code="ABC123",
            date_issued=datetime(2026, 8, 21, 20, 30, 0, tzinfo=timezone.utc),
            owner="Jane Jumper",
        )

        ticket.produce_pdf(output)

        pdf = output.getvalue().decode("latin1")
        self.assertTrue(pdf.startswith("%PDF-"))
        self.assertEqual(len(re.findall(r"/Type /Page\b", pdf)), 1)
        self.assertIn(f"/MediaBox [ 0 0 {PAGE_WIDTH:g} {PAGE_HEIGHT:g} ]", pdf)
        self.assertIn("(Jane Jumper) Tj", pdf)
        self.assertIn("(ABC123) Tj", pdf)
        self.assertIn("(2026-08-21 20:30:00 UTC) Tj", pdf)
        self.assertIn("(Issued: 2026-08-21 13:30:00 PDT) Tj", pdf)
        self.assertIn("(Skydive Toledo LLC jump ticket) Tj", pdf)
        self.assertIn("(One jump 36$) Tj", pdf)
        self.assertIn("(Paid with card xxxx-0000) Tj", pdf)
        self.assertIn("(To: Jane Jumper) Tj", pdf)
        self.assertIn("(Jumper:) Tj", pdf)
        self.assertIn("(_____________________) Tj", pdf)

    def test_naive_date_issued_is_treated_as_utc(self) -> None:
        output = BytesIO()
        ticket = Ticket(
            identifier="ticket-2",
            code="XYZ789",
            date_issued=datetime(2026, 1, 15, 12, 0, 0),
            owner="John Jumper",
        )

        ticket.produce_pdf(output)

        pdf = output.getvalue().decode("latin1")
        self.assertIn("(2026-01-15 12:00:00 UTC) Tj", pdf)
        self.assertIn("(Issued: 2026-01-15 05:00:00 PDT) Tj", pdf)


if __name__ == "__main__":
    unittest.main()
