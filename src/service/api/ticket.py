from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus

from ...model.auth import User
from ...model.ticket import Redemption, Ticket, UserRef
from ._shared import _json_response, _payload


def redeem_ticket(environ: dict, claims: dict, partner):
    values = _payload(environ, claims, ("code", "external_id"))
    code, external_id = values.get("code"), values.get("external_id")
    if not isinstance(code, str) or not isinstance(external_id, str):
        raise ValueError("code and external_id are required.")
    display_name = values.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise ValueError("display_name must be a string.")
    reason = values.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string.")

    ticket = Ticket.objects(code=code).first()
    if ticket is None:
        return _json_response(HTTPStatus.NOT_FOUND, {"error": "Ticket not found."})

    user = User.objects(**{f"partner_uid_map__{partner.id}": external_id}).first()
    redeemed_by = UserRef(
        id=user.id if user is not None else None,
        display_name=display_name if display_name is not None else (user.display_name if user is not None else None),
    )
    redeemed = Redemption(dt=datetime.now(timezone.utc), by=redeemed_by, reason=reason)
    updated_ticket = Ticket.objects(id=ticket.id, redeemed=None).modify(set__redeemed=redeemed, new=True)
    if updated_ticket is None:
        return _json_response(HTTPStatus.CONFLICT, {"error": "Ticket already redeemed."})

    return _json_response(
        HTTPStatus.CREATED,
        {
            "code": updated_ticket.code,
            "internal_id": str(updated_ticket.id),
            "redeemed": {
                "at": updated_ticket.redeemed.dt.isoformat() if updated_ticket.redeemed.dt else None,
                "by": {
                    "internal_id": str(updated_ticket.redeemed.by.id) if updated_ticket.redeemed.by.id is not None else None,
                    "display_name": updated_ticket.redeemed.by.display_name,
                },
                "reason": updated_ticket.redeemed.reason,
            },
        },
    )
