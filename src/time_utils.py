from __future__ import annotations

from datetime import datetime, timezone, tzinfo


DISPLAY_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def as_timezone(value: datetime, target_timezone: tzinfo) -> datetime:
    return as_utc(value).astimezone(target_timezone)


def format_datetime(value: datetime | None, target_timezone: tzinfo) -> str:
    if value is None:
        return ""
    return as_timezone(value, target_timezone).strftime(DISPLAY_DATETIME_FORMAT)
