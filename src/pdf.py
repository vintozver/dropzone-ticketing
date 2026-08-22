from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib import resources
from io import BytesIO
from typing import BinaryIO

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .model import ticket as _ticket


PAGE_WIDTH = 5.25 * inch
PAGE_HEIGHT = 1.93 * inch
LEFT_SECTION_WIDTH = 1.4 * inch
VERTICAL_MARGIN = 0.15 * inch
HORIZONTAL_MARGIN = 0.08 * inch
LOGO_RESOURCE = "logo.png"
LOGO_WIDTH = 1 * inch
PDT = timezone(timedelta(hours=-7), "PDT")


class PDF(object):
    """A PDF document rendering one page per appended ticket."""

    def __init__(self, destination: BinaryIO):
        self.canvas = canvas.Canvas(
            destination,
            pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
            pageCompression=0,
        )
        self.canvas.setTitle("Tickets")

    def append(self, ticket: _ticket.Ticket) -> None:
        """Render ``ticket`` onto a new page of the document."""
        issued_utc = ticket.issued_utc()
        issued_pdt = issued_utc.astimezone(PDT)

        self._draw_logo()
        self._draw_left_section(ticket, issued_utc)
        self._draw_right_section(ticket, issued_pdt)
        self._draw_qr_code(ticket)
        self.canvas.showPage()

    def render(self) -> None:
        self.canvas.save()

    def _draw_left_section(self, ticket: _ticket.Ticket, issued_utc: datetime) -> None:
        x = 0.08 * inch
        y = PAGE_HEIGHT - VERTICAL_MARGIN - 0.18 * inch
        line_height = 0.18 * inch

        self.canvas.setFont("Helvetica", 8)
        self.canvas.drawString(x, y, ticket.owner)
        self.canvas.drawString(x, y - line_height, ticket.code)
        self.canvas.drawString(x, y - 2 * line_height, self._format_datetime(issued_utc, "UTC"))

    def _draw_right_section(self, ticket: _ticket.Ticket, issued_pdt: datetime) -> None:
        x = LEFT_SECTION_WIDTH + 0.12 * inch
        y = PAGE_HEIGHT - VERTICAL_MARGIN - 0.12 * inch
        line_height = 0.16 * inch

        self.canvas.setFont("Helvetica-Bold", 16)
        self.canvas.drawString(x, y, "Skydive Toledo LLC")
        self.canvas.setFont("Helvetica", 12)

        lines = [
            ticket.purpose or "",
            f"Paid: {ticket.payment or ''}",
            f"Issued: {self._format_datetime(issued_pdt, 'PDT')}",
            f"To: {ticket.owner}",
            "Jumper:",
            "_____________________",
        ]
        for index, line in enumerate(lines, start=1):
            self.canvas.drawString(x, y - index * line_height, line)

    def _draw_qr_code(self, ticket: _ticket.Ticket) -> None:
        qr_code = QrCodeWidget(ticket.code)
        qr_size = 1.0 * inch
        bounds = qr_code.getBounds()
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]

        drawing = Drawing(qr_size, qr_size, transform=[qr_size / width, 0, 0, qr_size / height, 0, 0])
        drawing.add(qr_code)
        renderPDF.draw(drawing, self.canvas, PAGE_WIDTH - qr_size, VERTICAL_MARGIN)

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
        self.canvas.drawImage(
            logo,
            HORIZONTAL_MARGIN,
            VERTICAL_MARGIN,
            width=LOGO_WIDTH,
            height=logo_height,
            mask="auto"
        )

    @staticmethod
    def _format_datetime(value: datetime, timezone_label: str) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S ") + timezone_label


def load_logo_bytes() -> bytes:
    return resources.files(__package__).joinpath(LOGO_RESOURCE).read_bytes()
