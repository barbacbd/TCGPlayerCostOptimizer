"""Shared glue between a wants list and the optimizer, used by the CLI and GUI."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable, Sequence

from .client import TCGPlayerClient, TCGPlayerError
from .models import Condition, Listing, Requirement

# progress(step, total, label) -- called once per distinct product fetched.
ProgressFn = Callable[[int, int, str], None]


def resolve_name(client: TCGPlayerClient, product_id: int, current: str = "") -> str:
    """Official product name, keeping a user's shorthand if it looks deliberate.

    A typed name is preserved when it appears inside the official name (so
    "Pepita" survives), but replaced otherwise so a mislabelled row can't
    silently show the wrong card in a buy list.
    """
    try:
        info = client.product_info(product_id)
    except TCGPlayerError:
        return current
    if not current or current == f"Product {product_id}":
        return info.display
    if current.lower() in info.display.lower():
        return current
    return info.display


def gather_listings(
    client: TCGPlayerClient,
    requirements: Sequence[Requirement],
    listings_per_card: int | None = 60,
    filter_conditions: bool = True,
    progress: ProgressFn | None = None,
    update_names: bool = True,
) -> list[Listing]:
    """Fetch candidate listings, one API sweep per distinct product."""
    by_product: dict[int, list[Requirement]] = defaultdict(list)
    for requirement in requirements:
        by_product[requirement.product_id].append(requirement)

    listings: list[Listing] = []
    seen: set[int] = set()
    total = len(by_product)

    for step, (product_id, rows) in enumerate(by_product.items(), start=1):
        conditions: set[Condition] | None = None
        if filter_conditions:
            conditions = set()
            for row in rows:
                conditions.update(row.acceptable_conditions())

        languages = sorted({row.language for row in rows if row.language})
        printings = sorted({row.printing for row in rows if row.printing})
        # Only constrain printing at the API when every row for this product agrees.
        if len(printings) != len({row.printing for row in rows}):
            printings = []

        if progress:
            progress(step, total, rows[0].name or f"product {product_id}")

        if update_names:
            for row in rows:
                row.name = resolve_name(client, product_id, row.name)

        for listing in client.fetch_listings(
            product_id,
            conditions=conditions,
            printings=printings or None,
            languages=languages or ("English",),
            limit=listings_per_card,
        ):
            if listing.listing_id in seen:
                continue
            seen.add(listing.listing_id)
            listings.append(listing)

    return listings


def usable_listings(
    listings: Iterable[Listing], requirements: Sequence[Requirement]
) -> list[Listing]:
    return [l for l in listings if any(r.accepts(l) for r in requirements)]
