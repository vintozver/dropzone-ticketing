from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Union

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


PAGE_WIDTH = 5.25 * inch
PAGE_HEIGHT = 1.93 * inch
LEFT_SECTION_WIDTH = 1.4 * inch
VERTICAL_MARGIN = 0.15 * inch
HORIZONTAL_MARGIN = 0.08 * inch
LOGO_RESOURCE = "logo.png"
LOGO_WIDTH = 1 * inch
PDT = timezone(timedelta(hours=-7), "PDT")


@dataclass(frozen=True)
class Ticket:
    identifier: str
    code: str
    date_issued: datetime
    owner: str

    def append_pdf(self, destination: canvas.Canvas) -> None:
        issued_utc = self._issued_utc()
        issued_pdt = issued_utc.astimezone(PDT)

        self._draw_logo(destination)
        self._draw_left_section(destination, issued_utc)
        self._draw_right_section(destination, issued_pdt)
        self._draw_qr_code(destination)
        destination.showPage()

    def _issued_utc(self) -> datetime:
        if self.date_issued.tzinfo is None:
            return self.date_issued.replace(tzinfo=timezone.utc)
        return self.date_issued.astimezone(timezone.utc)

    def _draw_left_section(self, ticket_canvas: canvas.Canvas, issued_utc: datetime) -> None:
        x = 0.08 * inch
        y = PAGE_HEIGHT - VERTICAL_MARGIN - 0.18 * inch
        line_height = 0.18 * inch

        ticket_canvas.setFont("Helvetica", 8)
        ticket_canvas.drawString(x, y, self.owner)
        ticket_canvas.drawString(x, y - line_height, self.code)
        ticket_canvas.drawString(x, y - 2 * line_height, self._format_datetime(issued_utc, "UTC"))

    def _draw_right_section(self, ticket_canvas: canvas.Canvas, issued_pdt: datetime) -> None:
        x = LEFT_SECTION_WIDTH + 0.12 * inch
        y = PAGE_HEIGHT - VERTICAL_MARGIN - 0.12 * inch
        line_height = 0.16 * inch

        ticket_canvas.setFont("Helvetica-Bold", 16)
        ticket_canvas.drawString(x, y, "Skydive Toledo LLC")
        ticket_canvas.setFont("Helvetica", 12)

        lines = [
            "One jump 36$",
            "Paid with card xxxx-0000",
            f"Issued: {self._format_datetime(issued_pdt, 'PDT')}",
            f"To: {self.owner}",
            "Jumper:",
            "_____________________",
        ]
        for index, line in enumerate(lines, start=1):
            ticket_canvas.drawString(x, y - index * line_height, line)

    def _draw_qr_code(self, ticket_canvas: canvas.Canvas) -> None:
        qr_code = QrCodeWidget(self.code)
        qr_size = 1.0 * inch
        bounds = qr_code.getBounds()
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]

        drawing = Drawing(qr_size, qr_size, transform=[qr_size / width, 0, 0, qr_size / height, 0, 0])
        drawing.add(qr_code)
        renderPDF.draw(drawing, ticket_canvas, PAGE_WIDTH - qr_size, VERTICAL_MARGIN)

    @staticmethod
    def _draw_logo(ticket_canvas: canvas.Canvas) -> None:
        logo = ImageReader(BytesIO(load_logo_bytes()))
        width, height = logo.getSize()
        logo_height = LOGO_WIDTH * height / width
        ticket_canvas.drawImage(
            logo,
            PAGE_WIDTH - HORIZONTAL_MARGIN - LOGO_WIDTH,
            PAGE_HEIGHT - VERTICAL_MARGIN - logo_height,
            width=LOGO_WIDTH,
            height=logo_height,
            mask="auto",
        )
        ticket_canvas.drawImage(
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
