from __future__ import annotations

from datetime import datetime, tzinfo
from importlib import resources
from io import BytesIO
from typing import BinaryIO

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.code128 import Code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .model import ticket as _ticket
from .time_utils import DISPLAY_DATETIME_FORMAT, as_timezone


PAGE_WIDTH = 5.25 * inch
PAGE_HEIGHT = 1.93 * inch
LEFT_SECTION_WIDTH = 1.4 * inch
VERTICAL_MARGIN = 0.15 * inch
HORIZONTAL_MARGIN = 0.08 * inch
LOGO_RESOURCE = "logo.png"
LOGO_WIDTH = 1 * inch
QR_SIZE = 0.65 * inch
BARCODE_BAR_WIDTH = 0.016 * inch
BARCODE_HEIGHT = 0.3 * inch
BARCODE_QUIET_ZONE = 0.25 * inch


class PDF(object):
    """A PDF document rendering one page per appended ticket."""

    def __init__(self, destination: BinaryIO, local_timezone: tzinfo, business_name: str):
        self.canvas = canvas.Canvas(
            destination,
            pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
            pageCompression=0,
        )
        self.local_timezone = local_timezone
        self.business_name = business_name
        self.canvas.setTitle("Tickets")

    def append(self, ticket: _ticket.Ticket) -> None:
        """Render ``ticket`` onto a new page of the document."""
        issued_utc = ticket.issued_utc()
        issued_local = as_timezone(issued_utc, self.local_timezone)

        self._draw_logo()
        self._draw_left_section(ticket, issued_utc)
        self._draw_right_section(ticket, issued_local)
        self._draw_qr_code(ticket)
        self._draw_barcode(ticket)
        self.canvas.showPage()

    def render(self) -> None:
        self.canvas.save()

    def _draw_left_section(self, ticket: _ticket.Ticket, issued_utc: datetime) -> None:
        x = 0.08 * inch
        y = PAGE_HEIGHT - VERTICAL_MARGIN - 0.18 * inch
        line_height = 0.18 * inch

        self.canvas.setFont("Helvetica", 8)
        self.canvas.drawString(x, y, ticket.issued_to.display_name or "")
        self.canvas.drawString(x, y - line_height, f"{self._format_datetime(issued_utc)} UTC")

    def _draw_right_section(self, ticket: _ticket.Ticket, issued_local: datetime) -> None:
        x = LEFT_SECTION_WIDTH + 0.12 * inch
        y = PAGE_HEIGHT - VERTICAL_MARGIN - 0.12 * inch
        line_height = 0.16 * inch

        self.canvas.setFont("Helvetica-Bold", 16)
        self.canvas.drawString(x, y, self.business_name)
        self.canvas.setFont("Helvetica", 12)

        lines = [
            ticket.purpose,
            f"Paid: {ticket.payment}",
            f"Issued: {self._format_datetime(issued_local)}",
            f"To: {ticket.issued_to.display_name or ''}",
            "Jumper:",
            "_____________________",
        ]
        for index, line in enumerate(lines, start=1):
            self.canvas.drawString(x, y - index * line_height, line)

    def _draw_qr_code(self, ticket: _ticket.Ticket) -> None:
        qr_code = QrCodeWidget(ticket.code)
        bounds = qr_code.getBounds()
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]

        drawing = Drawing(QR_SIZE, QR_SIZE, transform=[QR_SIZE / width, 0, 0, QR_SIZE / height, 0, 0])
        drawing.add(qr_code)
        renderPDF.draw(drawing, self.canvas, (LEFT_SECTION_WIDTH - QR_SIZE) / 2, VERTICAL_MARGIN)

    def _draw_barcode(self, ticket: _ticket.Ticket) -> None:
        barcode = Code128(
            ticket.code,
            barWidth=BARCODE_BAR_WIDTH,
            barHeight=BARCODE_HEIGHT,
            quiet=True,
            lquiet=BARCODE_QUIET_ZONE,
            rquiet=BARCODE_QUIET_ZONE,
        )
        barcode.drawOn(
            self.canvas,
            LEFT_SECTION_WIDTH,
            VERTICAL_MARGIN,
        )

    def _draw_logo(self) -> None:
        logo = ImageReader(BytesIO(load_logo_bytes()))
        width, height = logo.getSize()
        logo_height = LOGO_WIDTH * height / width
        self.canvas.drawImage(
            logo,
            PAGE_WIDTH - HORIZONTAL_MARGIN - LOGO_WIDTH,
            PAGE_HEIGHT - VERTICAL_MARGIN - logo_height,
            width=LOGO_WIDTH,
            height=logo_height,
            mask="auto",
        )

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return value.strftime(DISPLAY_DATETIME_FORMAT)


def load_logo_bytes() -> bytes:
    return resources.files(__package__).joinpath(LOGO_RESOURCE).read_bytes()
