from __future__ import annotations


def view_owners(*, ticket_class, render):
    registered_owners = []
    for user_id in sorted(ticket_class.objects(redeemed=None, issued_to__id__ne=None).distinct("issued_to.id"), key=str):
        sample = ticket_class.objects(redeemed=None, issued_to__id=user_id).only("issued_to").first()
        display_name = getattr(getattr(sample, "issued_to", None), "display_name", None)
        registered_owners.append({"id": str(user_id), "display_name": display_name or str(user_id)})

    unregistered_owners = sorted(
        name
        for name in ticket_class.objects(redeemed=None, issued_to__id=None).distinct("issued_to.display_name")
        if name
    )
    return render(
        "tickets.html",
        registered_owners=registered_owners,
        unregistered_owners=unregistered_owners,
    )
