from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus

from bson import ObjectId
from bson.errors import InvalidId


def _parse_event_id(event_id: str):
    try:
        return ObjectId(event_id.strip())
    except (InvalidId, TypeError):
        return None


def _event_context(event, viewer: dict[str, object]) -> dict[str, object]:
    viewer_id = viewer.get("id")
    bookings = list(event.bookings or [])
    return {
        "event": {
            "id": str(event.id),
            "name": event.name,
            "description": event.description or "",
            "starts": event.starts,
            "capacity": event.capacity,
            "booked": len(bookings),
            "available": max(0, event.capacity - len(bookings)),
            "is_booked": any(booking.user.id == viewer_id for booking in bookings),
            "bookings": bookings,
        },
        "is_admin": "admin" in viewer.get("roles", []),
    }


def create_event(
    form: dict[str, str],
    created_by: dict[str, object] | None,
    *,
    event_class,
    user_ref_class,
    render,
    local_timezone,
    now=None,
):
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    starts_value = form.get("starts", "").strip()
    capacity_value = form.get("capacity", "").strip()
    values = {
        "name": name,
        "description": description,
        "starts": starts_value,
        "capacity": capacity_value,
    }

    if not name:
        return render("event_create.html", HTTPStatus.BAD_REQUEST, error="Name is required.", **values)
    try:
        starts = datetime.fromisoformat(starts_value)
    except ValueError:
        return render(
            "event_create.html",
            HTTPStatus.BAD_REQUEST,
            error="Start time is required.",
            **values,
        )
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=local_timezone)
    starts = starts.astimezone(timezone.utc)
    current_time = now or datetime.now(timezone.utc)
    if starts <= current_time:
        return render(
            "event_create.html",
            HTTPStatus.BAD_REQUEST,
            error="Start time must be in the future.",
            **values,
        )
    try:
        capacity = int(capacity_value)
    except ValueError:
        capacity = 0
    if capacity < 1:
        return render(
            "event_create.html",
            HTTPStatus.BAD_REQUEST,
            error="Capacity must be at least 1.",
            **values,
        )
    if created_by is None:
        return render("error.html", HTTPStatus.FORBIDDEN, message="Authentication required.")

    event = event_class(
        name=name,
        description=description or None,
        starts=starts,
        capacity=capacity,
        created_by=user_ref_class(
            id=created_by.get("id"),
            display_name=str(created_by.get("display_name", "")).strip() or None,
        ),
    )
    event.save()
    return HTTPStatus.SEE_OTHER, [("Location", f"/event/{event.id}")], b""


def list_events(viewer: dict[str, object], *, event_class, render, now=None):
    current_time = now or datetime.now(timezone.utc)
    events = []
    for event in event_class.objects(starts__gte=current_time).order_by("starts"):
        bookings = list(event.bookings or [])
        events.append(
            {
                "id": str(event.id),
                "name": event.name,
                "starts": event.starts,
                "available": max(0, event.capacity - len(bookings)),
                "capacity": event.capacity,
                "is_booked": any(booking.user.id == viewer.get("id") for booking in bookings),
            }
        )
    return render("events.html", events=events)


def view_event(event_id: str, viewer: dict[str, object], *, event_class, render):
    object_id = _parse_event_id(event_id)
    if object_id is None:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid event identifier.")
    event = event_class.objects(id=object_id).first()
    if event is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Event not found.")
    return render("event.html", **_event_context(event, viewer))


def update_booking(
    event_id: str,
    action: str,
    viewer: dict[str, object],
    *,
    event_class,
    booking_class,
    user_ref_class,
    render,
    now=None,
):
    object_id = _parse_event_id(event_id)
    if object_id is None:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Invalid event identifier.")
    if action not in {"book", "cancel"}:
        return render("error.html", HTTPStatus.BAD_REQUEST, message="Choose a booking action.")

    current_time = now or datetime.now(timezone.utc)
    for _attempt in range(5):
        event = event_class.objects(id=object_id).first()
        if event is None:
            return render("error.html", HTTPStatus.NOT_FOUND, message="Event not found.")
        if event.starts <= current_time:
            return render(
                "event.html",
                HTTPStatus.CONFLICT,
                error="This event has already started.",
                **_event_context(event, viewer),
            )

        bookings = list(event.bookings or [])
        existing = next(
            (booking for booking in bookings if booking.user.id == viewer.get("id")),
            None,
        )
        if action == "book":
            if existing is not None:
                return render(
                    "event.html",
                    HTTPStatus.CONFLICT,
                    error="You have already booked this event.",
                    **_event_context(event, viewer),
                )
            if len(bookings) >= event.capacity:
                return render(
                    "event.html",
                    HTTPStatus.CONFLICT,
                    error="This event is fully booked.",
                    **_event_context(event, viewer),
                )
            booking = booking_class(
                user=user_ref_class(
                    id=viewer.get("id"),
                    display_name=str(viewer.get("display_name", "")).strip() or None,
                ),
                dt=current_time,
            )
            updated = event_class.objects(
                id=object_id,
                bookings__size=len(bookings),
            ).modify(new=True, push__bookings=booking)
        else:
            if existing is None:
                return render(
                    "event.html",
                    HTTPStatus.CONFLICT,
                    error="You do not have a booking for this event.",
                    **_event_context(event, viewer),
                )
            updated = event_class.objects(
                id=object_id,
                bookings__size=len(bookings),
            ).modify(new=True, pull__bookings=existing)
        if updated is not None:
            return HTTPStatus.SEE_OTHER, [("Location", f"/event/{event_id}")], b""

    return render(
        "error.html",
        HTTPStatus.CONFLICT,
        message="The booking changed concurrently. Please try again.",
    )
