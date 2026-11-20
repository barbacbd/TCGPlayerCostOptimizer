"""Core data types: card conditions, marketplace listings, and purchase requirements."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum

# ---------------------------------------------------------------------------
# Shipping model
#
# These constants were derived empirically from 1,098 live TCGPlayer listings
# (see README "How shipping is modelled"). The rule matched 1098/1098 samples:
#
#     shipping(order) = seller_rate                      if subtotal >= $5.00
#                       max(seller_rate, $1.49)          if subtotal <  $5.00
#
# where ``seller_rate`` is the listing's ``sellerShippingPrice`` (0.00 for the
# majority of sellers, who ride TCGPlayer's "free shipping on $5+" program).
# Shipping is charged once per seller, per order -- which is precisely what
# makes consolidating across sellers worth optimizing.
# ---------------------------------------------------------------------------

FREE_SHIPPING_THRESHOLD = 5.00
MIN_SHIPPING = 1.49


def order_shipping(
    seller_rate: float,
    subtotal: float,
    threshold: float = FREE_SHIPPING_THRESHOLD,
    floor: float = MIN_SHIPPING,
) -> float:
    """Shipping charged for a single order of ``subtotal`` from one seller."""
    if subtotal + 1e-9 >= threshold:
        return round(seller_rate, 2)
    return round(max(seller_rate, floor), 2)


class Condition(IntEnum):
    """Card condition, ordered best (NM = 0) to worst (DMG = 4)."""

    NM = 0
    LP = 1
    MP = 2
    HP = 3
    DMG = 4

    @property
    def api_name(self) -> str:
        return _API_NAMES[self]

    @property
    def label(self) -> str:
        return self.name

    def is_at_least(self, worst_acceptable: "Condition") -> bool:
        """True if this condition is as good as, or better than, ``worst_acceptable``."""
        return self.value <= worst_acceptable.value

    @classmethod
    def parse(cls, raw: str) -> "Condition":
        key = re.sub(r"[^a-z]", "", str(raw).lower())
        try:
            return _ALIASES[key]
        except KeyError:
            raise ValueError(
                f"Unknown condition {raw!r}. Use one of: NM, LP, MP, HP, DMG "
                f"(or the full names 'Near Mint', 'Lightly Played', ...)."
            ) from None


_API_NAMES = {
    Condition.NM: "Near Mint",
    Condition.LP: "Lightly Played",
    Condition.MP: "Moderately Played",
    Condition.HP: "Heavily Played",
    Condition.DMG: "Damaged",
}

_ALIASES = {
    "nm": Condition.NM,
    "n": Condition.NM,
    "nearmint": Condition.NM,
    "mint": Condition.NM,
    "lp": Condition.LP,
    "lightlyplayed": Condition.LP,
    "slightlyplayed": Condition.LP,
    "sp": Condition.LP,
    "mp": Condition.MP,
    "moderatelyplayed": Condition.MP,
    "hp": Condition.HP,
    "heavilyplayed": Condition.HP,
    "dmg": Condition.DMG,
    "d": Condition.DMG,
    "damaged": Condition.DMG,
    "poor": Condition.DMG,
}

# Reverse lookup for API condition strings.
CONDITION_FROM_API = {v.lower(): k for k, v in _API_NAMES.items()}


def condition_from_api(name: str) -> Condition | None:
    """Map a TCGPlayer condition string to a Condition, or None if unrecognised.

    Sealed product listings use conditions like "Unopened" which have no
    play-condition equivalent; those return None and are skipped.
    """
    return CONDITION_FROM_API.get(str(name).strip().lower())


@dataclass(frozen=True)
class Listing:
    """A single seller's offer for one product in one condition."""

    listing_id: int
    product_id: int
    seller_id: str
    seller_name: str
    price: float
    quantity: int
    condition: Condition
    printing: str
    language: str
    seller_shipping_price: float
    shipping_price: float
    seller_rating: float | None = None
    seller_sales: str | None = None
    direct_seller: bool = False
    gold_seller: bool = False
    verified_seller: bool = False
    listing_type: str = "standard"

    @property
    def solo_total(self) -> float:
        """Cost to buy exactly one copy of this listing on its own order."""
        return round(self.price + order_shipping(self.seller_shipping_price, self.price), 2)


@dataclass(frozen=True)
class SellerTerms:
    seller_id: str
    seller_name: str
    rate: float

    def shipping_for(self, subtotal: float) -> float:
        return order_shipping(self.rate, subtotal)


@dataclass
class Requirement:
    """One row of the wants list: N copies of a product at a given condition."""

    row: int
    product_id: int
    quantity: int
    condition: Condition
    condition_mode: str = "min"  # "min" = this condition or better; "exact"
    printing: str | None = None
    language: str = "English"
    name: str = ""
    url: str = ""
    needed: int = 0
    has: int = 0

    def __post_init__(self) -> None:
        if self.condition_mode not in ("min", "exact"):
            raise ValueError(
                f"condition_mode must be 'min' or 'exact', got {self.condition_mode!r}"
            )

    @property
    def label(self) -> str:
        base = self.name or f"Product {self.product_id}"
        suffix = self.condition.label
        if self.condition_mode == "min" and self.condition is not Condition.DMG:
            suffix += "+"
        if self.printing:
            suffix += f", {self.printing}"
        return f"{base} [{suffix}]"

    def accepts(self, listing: Listing) -> bool:
        if listing.product_id != self.product_id:
            return False
        if self.printing and listing.printing.lower() != self.printing.lower():
            return False
        if self.language and listing.language.lower() != self.language.lower():
            return False
        if self.condition_mode == "exact":
            return listing.condition is self.condition
        return listing.condition.is_at_least(self.condition)

    def acceptable_conditions(self) -> list[Condition]:
        if self.condition_mode == "exact":
            return [self.condition]
        return [c for c in Condition if c.is_at_least(self.condition)]


@dataclass
class Allocation:
    """``quantity`` copies of ``listing`` bought to satisfy ``requirement``."""

    listing: Listing
    requirement: Requirement
    quantity: int

    @property
    def cost(self) -> float:
        return round(self.listing.price * self.quantity, 2)


@dataclass
class SellerOrder:
    seller_id: str
    seller_name: str
    allocations: list[Allocation] = field(default_factory=list)
    shipping: float = 0.0
    seller_rate: float = 0.0

    @property
    def subtotal(self) -> float:
        return round(sum(a.cost for a in self.allocations), 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.shipping, 2)

    @property
    def item_count(self) -> int:
        return sum(a.quantity for a in self.allocations)

    @property
    def free_shipping_gap(self) -> float:
        """How much more spend would unlock free shipping, or 0 if not applicable."""
        if self.seller_rate > 0 or self.shipping == 0:
            return 0.0
        return round(max(0.0, FREE_SHIPPING_THRESHOLD - self.subtotal), 2)


@dataclass
class Plan:
    orders: list[SellerOrder] = field(default_factory=list)
    unfilled: list[tuple[Requirement, int]] = field(default_factory=list)
    status: str = "Optimal"

    @property
    def items_total(self) -> float:
        return round(sum(o.subtotal for o in self.orders), 2)

    @property
    def shipping_total(self) -> float:
        return round(sum(o.shipping for o in self.orders), 2)

    @property
    def grand_total(self) -> float:
        return round(self.items_total + self.shipping_total, 2)

    @property
    def card_count(self) -> int:
        return sum(o.item_count for o in self.orders)

    @property
    def is_complete(self) -> bool:
        return not self.unfilled
