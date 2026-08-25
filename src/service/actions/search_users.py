from __future__ import annotations

from mongoengine.queryset.visitor import Q

def search_users(query: str, *, user_class) -> list[dict[str, str]]:
    query = query.strip()
    if not query:
        return []

    users = list(
        user_class.objects(Q(display_name__istartswith=query))
        .only("id", "display_name")
        .limit(10)
    )
    if len(users) < 10:
        seen_ids = [user.id for user in users]
        users.extend(
            user_class.objects((Q(display_name__icontains=query)), id__nin=seen_ids)
            .only("id", "display_name")
            .limit(10 - len(users))
        )

    return [
        {
            "id": str(user.id),
            "display_name": user.display_name or str(user.id),
        }
        for user in users[:10]
    ]
