from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Ticket:
    """A single issued ticket, as rendered onto a PDF page."""

    identifier: str
    code: str
    date_issued: datetime
    owner: str

    def issued_utc(self) -> datetime:
        """Return ``date_issued`` in UTC, treating naive values as UTC."""
        if self.date_issued.tzinfo is None:
            return self.date_issued.replace(tzinfo=timezone.utc)
        return self.date_issued.astimezone(timezone.utc)
