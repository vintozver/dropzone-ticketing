from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus

from dropzone_ticketing.model.ticket import Redemption


def redeem(form: dict[str, str], *, ticket_class, render, split_codes, by_user: str | None = None):
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
            ticket.redeemed = Redemption(dt=datetime.now(timezone.utc), by_user=by_user, reason=reason)
            ticket.save()
            results.append({"code": code, "result": "redeemed OK"})

    return render("redeemed.html", results=results)
