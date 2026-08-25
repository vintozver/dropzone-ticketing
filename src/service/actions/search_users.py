from __future__ import annotations


def search_users(query: str, *, user_class) -> list[dict[str, str]]:
    query = query.strip()
    if not query:
        return []

    users = list(user_class.objects(display_name__istartswith=query).only("id", "login", "display_name").limit(10))
    if len(users) < 10:
        seen_ids = [user.id for user in users]
        users.extend(
            user_class.objects(display_name__icontains=query, id__nin=seen_ids)
            .only("id", "login", "display_name")
            .limit(10 - len(users))
        )

    return [
        {
            "id": str(user.id),
            "display_name": user.display_name or user.login,
        }
        for user in users[:10]
    ]
