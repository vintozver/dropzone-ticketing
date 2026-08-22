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

from . import ticket as _ticket


PAGE_WIDTH = 5.25 * inch
PAGE_HEIGHT = 1.93 * inch


class PDF(object):
    def __init__(self, destination: BytesIO):
        self.canvas = canvas.Canvas(
            destination,
            pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
            pageCompression=0,
        )
        self.canvas.setTitle(f"Tickets")

    def append(self, ticket: _ticket.Ticket):
        ticket.append_pdf(self.canvas)

    def render(self):
        self.canvas.save()

