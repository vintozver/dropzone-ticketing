import datetime
import http.server
import io
import socketserver
import os
import socket
from zoneinfo import ZoneInfo

import bson

import dropzone_ticketing
from dropzone_ticketing.model.ticket import UserRef


class SingleFileHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        pdf_io = io.BytesIO()
        pdf = dropzone_ticketing.PDF(pdf_io, local_timezone=ZoneInfo("UTC"), business_name="The Dropzone")

        dt = datetime.datetime.now(datetime.UTC)
        t = dropzone_ticketing.Ticket(
            id=bson.ObjectId.from_datetime(dt),
            code='*CHEX#',
            issued_to=UserRef(display_name='Big Boy'),
            issued_by=UserRef(display_name='issuer'),
            payment='cash',
            purpose='jump',
        )
        pdf.append(t)
        t = dropzone_ticketing.Ticket(
            id=bson.ObjectId.from_datetime(dt),
            code='*XXXX#',
            issued_to=UserRef(display_name='Big Boy'),
            issued_by=UserRef(display_name='issuer'),
            payment='cash',
            purpose='jump',
        )
        pdf.append(t)

        pdf.render()
        pdf_io.seek(0, 0)

        # Send a successful HTTP 200 response
        self.send_response(200)
        
        # Set the correct content type (optional: change based on your file type)
        self.send_header("Content-type", "application/pdf")
        self.end_headers()
        self.wfile.write(pdf_io.read())


class TheServer(socketserver.TCPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True


if __name__ == "__main__":
    # Start the server
    with TheServer(("::", 8080), SingleFileHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
