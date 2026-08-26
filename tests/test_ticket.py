from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

from bson import ObjectId

from dropzone_ticketing import PDF, Ticket
from dropzone_ticketing.model.ticket import UserRef
from dropzone_ticketing.pdf import (
    PAGE_HEIGHT,
    PAGE_WIDTH,
    load_logo_bytes,
)


def make_ticket(
    code: str,
    owner: str,
    issued: datetime,
    payment: str = "cash",
    purpose: str = "One jump 36$",
) -> Ticket:
    """Build an unsaved ticket whose id encodes the given issue timestamp."""
    return Ticket(
        id=ObjectId.from_datetime(issued),
        code=code,
        issued_to=UserRef(display_name=owner),
        issued_by=UserRef(display_name="issuer"),
        payment=payment,
        purpose=purpose,
    )


class TicketPdfTest(unittest.TestCase):
    def test_ticket_produces_single_page_pdf_with_expected_size_and_text(self) -> None:
        output = BytesIO()
        ticket = make_ticket(
            "ABC123",
            "Jane Jumper",
            datetime(2026, 8, 21, 20, 30, 0, tzinfo=timezone.utc),
            payment="credit card xxxx-1234",
            purpose="King Air full altitude 36$",
        )

        pdf = PDF(output, local_timezone=ZoneInfo("America/Los_Angeles"))
        pdf.append(ticket)
        pdf.render()

        rendered = output.getvalue().decode("latin1")
        self.assertTrue(rendered.startswith("%PDF-"))
        self.assertEqual(len(re.findall(r"/Type /Page\b", rendered)), 1)
        self.assertIn(f"/MediaBox [ 0 0 {PAGE_WIDTH:g} {PAGE_HEIGHT:g} ]", rendered)
        self.assertIn("(Jane Jumper) Tj", rendered)
        self.assertIn("(ABC123) Tj", rendered)
        self.assertIn("(2026-08-21 20:30:00 UTC) Tj", rendered)
        self.assertIn("(Issued: 2026-08-21 13:30:00) Tj", rendered)
        self.assertNotIn("(Issued: 2026-08-21 13:30:00 PDT) Tj", rendered)
        self.assertIn("(The Dropzone) Tj", rendered)
        self.assertIn("(King Air full altitude 36$) Tj", rendered)
        self.assertIn("(Paid: credit card xxxx-1234) Tj", rendered)
        self.assertIn("(To: Jane Jumper) Tj", rendered)
        self.assertIn("(Jumper:) Tj", rendered)
        self.assertIn("(_____________________) Tj", rendered)
        self.assertNotIn("One jump 36$", rendered)
        self.assertNotIn("Paid with card xxxx-0000", rendered)

    def test_ticket_pdf_uses_configured_business_name(self) -> None:
        output = BytesIO()
        ticket = make_ticket(
            "ABC123",
            "Jane Jumper",
            datetime(2026, 8, 21, 20, 30, 0, tzinfo=timezone.utc),
        )

        pdf = PDF(output, business_name="Skydive Example")
        pdf.append(ticket)
        pdf.render()

        rendered = output.getvalue().decode("latin1")
        self.assertIn("(Skydive Example) Tj", rendered)
        self.assertNotIn("(The Dropzone) Tj", rendered)

    def test_issue_timestamp_is_derived_from_the_identifier(self) -> None:
        issued = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        ticket = make_ticket("XYZ789", "John Jumper", issued)

        self.assertEqual(ticket.issued_utc(), issued)

    def test_issued_utc_requires_an_identifier(self) -> None:
        ticket = Ticket(
            code="NOID01",
            issued_to=UserRef(display_name="John Jumper"),
            issued_by=UserRef(display_name="issuer"),
            payment="cash",
            purpose="One jump 36$",
        )

        with self.assertRaises(ValueError):
            ticket.issued_utc()


class TicketLogoTest(unittest.TestCase):
    def test_logo_resource_is_available_as_png(self) -> None:
        logo = load_logo_bytes()

        self.assertTrue(logo.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_ticket_pdf_embeds_the_logo_image(self) -> None:
        output = BytesIO()
        ticket = make_ticket(
            "LOGO01",
            "Jane Jumper",
            datetime(2026, 8, 21, 20, 30, 0, tzinfo=timezone.utc),
        )

        pdf = PDF(output)
        pdf.append(ticket)
        pdf.render()

        rendered = output.getvalue().decode("latin1")
        self.assertIn("/Subtype /Image", rendered)


if __name__ == "__main__":
    unittest.main()
