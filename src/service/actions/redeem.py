from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus

from dropzone_ticketing.model.ticket import Redemption


def redeem(form: dict[str, str], *, ticket_class, render, split_codes, user_ref_class, by: dict[str, object] | None = None):
    codes = split_codes(form.get("codes", ""))
    reason = form.get("reason", "").strip() or None
    if not codes:
        return render("redeem.html", HTTPStatus.BAD_REQUEST, error="Enter at least one ticket code.")

    results = []
    for code in codes:
        ticket = ticket_class.objects(code=code).first()
        if ticket is None:
            results.append({"code": code, "result": "not found"})
        elif ticket.redeemed is not None:
            results.append({"code": code, "result": "already redeemed", "redeemed": ticket.redeemed})
        else:
            redeemed_by = None
            if by is not None:
                redeemed_by = user_ref_class(
                    id=by.get("id"),
                    display_name=str(by.get("display_name", "")).strip() or None,
                )
            ticket.redeemed = Redemption(dt=datetime.now(timezone.utc), by=redeemed_by, reason=reason)
            ticket.save()
            results.append({"code": code, "result": "redeemed OK"})

    return render("redeemed.html", results=results)
