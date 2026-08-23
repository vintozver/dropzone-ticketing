from __future__ import annotations


def view_owners(*, ticket_class, render):
    owners = sorted(ticket_class.objects(redeemed=None).distinct("owner"))
    return render("tickets.html", owners=owners)
