"""The grocery list as heb.com search links, for adding items to the HEB cart by hand.

The bot never talks to HEB: each link only opens heb.com's own search for that item
(https://www.heb.com/search/?q=<item>), and the person adds it to the cart there.
"""

from __future__ import annotations

from urllib.parse import quote

HEB_SEARCH_URL = "https://www.heb.com/search/?q="


def heb_search_url(name: str) -> str:
    return HEB_SEARCH_URL + quote(" ".join(name.split()), safe="")


def render_shopping_links(list_name: str, items: list[dict]) -> str:
    """Items come straight from the Yuvomi read (name, quantity)."""
    named = [i for i in items if (i.get("name") or "").strip()]
    if not named:
        return f'The "{list_name}" list is empty, so there is nothing to shop for.'
    count = f"{len(named)} item{'s' if len(named) != 1 else ''}"
    lines = [f"{list_name} ({count}). Tap a link to find it on heb.com and add it to your cart:"]
    for n, item in enumerate(named, 1):
        name = " ".join(item["name"].split())
        qty = str(item.get("quantity") or "").strip()
        label = f"{name} (x{qty})" if qty.isdigit() and qty != "1" else f"{name} ({qty})" if qty and qty != "1" else name
        lines.append(f"{n}. {label}\n{heb_search_url(name)}")
    lines.append("The bot does not touch your HEB cart. You check out in the HEB app as usual.")
    return "\n".join(lines)
