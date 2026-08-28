from __future__ import annotations

import secrets
import smtplib
from email.message import EmailMessage

from .config import email_from_address, email_from_name, email_smtp


def send_code(recipient: str, code: str) -> None:
    sender = email_from_address()
    if not email_smtp() or not sender:
        raise ValueError("Email authentication is not configured.")
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = f"{email_from_name()} <{sender}>" if email_from_name() else sender
    message["Subject"] = "Your authentication code"
    message.set_content(f"Your authentication code is {code}. It expires in 5 minutes.")
    smtp_server = email_smtp()
    host, separator, port = smtp_server.rpartition(":")
    smtp_args = (host, int(port)) if separator and port.isdigit() else (smtp_server,)
    with smtplib.SMTP(*smtp_args) as smtp:
        smtp.starttls()
        smtp.send_message(message)


def code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"
