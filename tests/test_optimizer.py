"""Correctness tests for the shipping model and the MILP, using synthetic data.

No network access: every listing here is constructed by hand so the expected
optimum can be reasoned about (or brute-forced) independently.
"""

from __future__ import annotations

import itertools
import random

import pytest

from tcgopt.models import Condition, Listing, Requirement, order_shipping
from tcgopt.optimizer import naive_plan, optimize

SELLER_SEQ = itertools.count(1)


def listing(
    seller: str,
    price: float,
    qty: int = 1,
    product: int = 1,
    condition: Condition = Condition.NM,
    rate: float = 0.0,
    printing: str = "Normal",
    language: str = "English",
) -> Listing:
    return Listing(
        listing_id=next(SELLER_SEQ),
        product_id=product,
        seller_id=seller,
        seller_name=f"Seller {seller}",
        price=price,
        quantity=qty,
        condition=condition,
        printing=printing,
        language=language,
        seller_shipping_price=rate,
        shipping_price=order_shipping(rate, price),
    )


def want(product: int, qty: int, condition: Condition = Condition.NM, mode: str = "min",
         row: int = 0, printing: str | None = None) -> Requirement:
    return Requirement(
        row=row or product,
        product_id=product,
        quantity=qty,
        condition=condition,
        condition_mode=mode,
        printing=printing,
        name=f"Card {product}",
    )


# --------------------------------------------------------------------------
# Shipping model (empirically derived from live listings; see README)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rate,subtotal,expected",
    [
        (0.0, 0.01, 1.49),    # cheap single card, free-shipping seller
        (0.0, 4.99, 1.49),    # just under the line
        (0.0, 5.00, 0.00),    # exactly at the line -> free
        (0.0, 60.00, 0.00),
        (0.99, 125.00, 0.99),  # flat-rate seller charges regardless of total
        (0.99, 1.00, 1.49),    # ...but never less than the $1.49 floor
        (4.99, 2.00, 4.99),
        (1.29, 249.99, 1.29),
    ],
)
def test_shipping_rule(rate, subtotal, expected):
    assert order_shipping(rate, subtotal) == pytest.approx(expected)


# --------------------------------------------------------------------------
# Core optimizer behaviour
# --------------------------------------------------------------------------

def test_consolidates_to_one_seller_when_shipping_dominates():
    """Two cards: cheapest-each costs two shipments; one seller is cheaper."""
    listings = [
        listing("A", 1.00, product=1),
        listing("B", 1.10, product=1),
        listing("C", 1.00, product=2),
        listing("B", 1.10, product=2),
    ]
    reqs = [want(1, 1), want(2, 1)]
    plan = optimize(reqs, listings)

    # Split: 1.00 + 1.49 + 1.00 + 1.49 = 4.98
    # Consolidated at B: 1.10 + 1.10 + 1.49 = 3.69
    assert len(plan.orders) == 1
    assert plan.orders[0].seller_name == "Seller B"
    assert plan.grand_total == pytest.approx(3.69)


def test_prefers_split_when_prices_dominate_shipping():
    """When price gaps exceed the extra shipping, splitting is correct."""
    listings = [
        listing("A", 1.00, product=1),
        listing("B", 10.00, product=1),
        listing("C", 1.00, product=2),
        listing("B", 10.00, product=2),
    ]
    plan = optimize([want(1, 1), want(2, 1)], listings)
    assert len(plan.orders) == 2
    assert plan.grand_total == pytest.approx(1.00 + 1.49 + 1.00 + 1.49)


def test_crosses_free_shipping_threshold():
    """Paying more per card can win by clearing the $5 free-shipping line."""
    listings = [
        listing("cheap", 2.00, qty=2, product=1),   # 2x2.00 = 4.00 -> +1.49 ship
        listing("pricey", 2.60, qty=2, product=1),  # 2x2.60 = 5.20 -> free
    ]
    plan = optimize([want(1, 2)], listings)
    assert plan.orders[0].seller_name == "Seller pricey"
    assert plan.shipping_total == 0.0
    assert plan.grand_total == pytest.approx(5.20)  # vs 5.49 from the cheap seller


def test_beats_greedy_on_threshold():
    """The naive cheapest-per-card baseline is strictly worse here."""
    listings = [
        listing("cheap", 2.00, qty=2, product=1),
        listing("pricey", 2.60, qty=2, product=1),
    ]
    reqs = [want(1, 2)]
    assert optimize(reqs, listings).grand_total < naive_plan(reqs, listings).grand_total


def test_respects_listing_quantity():
    listings = [
        listing("A", 1.00, qty=2, product=1),
        listing("A", 1.50, qty=5, product=1),
    ]
    plan = optimize([want(1, 4)], listings)
    allocations = {a.listing.price: a.quantity for a in plan.orders[0].allocations}
    assert allocations == {1.00: 2, 1.50: 2}
    assert plan.card_count == 4


def test_flat_rate_seller_never_gets_free_shipping():
    listings = [listing("flat", 50.00, rate=0.99, product=1)]
    plan = optimize([want(1, 1)], listings)
    assert plan.shipping_total == pytest.approx(0.99)
    assert plan.grand_total == pytest.approx(50.99)


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------

def test_min_condition_accepts_better_grades():
    listings = [
        listing("A", 5.00, product=1, condition=Condition.NM),
        listing("B", 3.00, product=1, condition=Condition.MP),
    ]
    plan = optimize([want(1, 1, Condition.MP)], listings)  # MP or better
    assert plan.orders[0].allocations[0].listing.condition is Condition.MP

    plan_nm = optimize([want(1, 1, Condition.NM)], listings)  # NM only
    assert plan_nm.orders[0].allocations[0].listing.condition is Condition.NM


def test_exact_condition_rejects_other_grades():
    listings = [
        listing("A", 5.00, product=1, condition=Condition.NM),
        listing("B", 3.00, product=1, condition=Condition.LP),
    ]
    plan = optimize([want(1, 1, Condition.LP, mode="exact")], listings)
    assert plan.orders[0].allocations[0].listing.condition is Condition.LP


def test_two_rows_same_card_different_conditions_share_stock():
    """A 2-copy listing can cover two different rows of the same card."""
    listings = [listing("A", 4.00, qty=2, product=1, condition=Condition.NM)]
    reqs = [
        want(1, 1, Condition.NM, row=2),
        want(1, 1, Condition.LP, row=3),  # LP-or-better: NM qualifies
    ]
    plan = optimize(reqs, listings)
    assert plan.is_complete
    assert plan.card_count == 2


def test_condition_ordering():
    assert Condition.NM < Condition.LP < Condition.MP < Condition.HP < Condition.DMG
    assert Condition.NM.is_at_least(Condition.DMG)
    assert not Condition.DMG.is_at_least(Condition.NM)
    assert Condition.parse("Near Mint") is Condition.NM
    assert Condition.parse("lightly played") is Condition.LP
    assert Condition.parse("dmg") is Condition.DMG
    with pytest.raises(ValueError):
        Condition.parse("pristine")


# --------------------------------------------------------------------------
# Shortfalls, caps, extras
# --------------------------------------------------------------------------

def test_reports_unfilled_when_stock_is_short():
    listings = [listing("A", 1.00, qty=2, product=1)]
    plan = optimize([want(1, 5)], listings)
    assert not plan.is_complete
    assert plan.unfilled[0][1] == 3
    assert plan.card_count == 2
    assert plan.grand_total < 100  # penalty must not leak into the reported cost


def test_no_listings_at_all():
    plan = optimize([want(1, 2)], [])
    assert plan.unfilled == [(plan.unfilled[0][0], 2)]
    assert plan.orders == []


def test_max_sellers_cap_is_enforced():
    listings = [
        listing("A", 1.00, product=1),
        listing("B", 1.00, product=2),
        listing("C", 1.00, product=3),
        listing("D", 3.00, qty=3, product=1),
        listing("D", 3.00, qty=3, product=2),
        listing("D", 3.00, qty=3, product=3),
    ]
    reqs = [want(1, 1), want(2, 1), want(3, 1)]
    plan = optimize(reqs, listings, max_sellers=1)
    assert len(plan.orders) == 1
    assert plan.is_complete


def test_allow_extra_buys_spare_to_unlock_free_shipping():
    """A $0.40 spare copy is cheaper than the $1.49 shipping it avoids.

    Order lands at $4.90 -- ten cents short of free shipping. Buying one extra
    copy of the cheap card costs $0.40 and saves the whole $1.49 charge.
    """
    listings = [
        listing("A", 4.50, qty=1, product=1),
        listing("A", 0.40, qty=2, product=2),
    ]
    reqs = [want(1, 1, row=1), want(2, 1, row=2)]

    strict = optimize(reqs, listings)
    assert strict.card_count == 2
    assert strict.grand_total == pytest.approx(4.90 + 1.49)

    extra = optimize(reqs, listings, allow_extra=True)
    assert extra.card_count == 3
    assert extra.shipping_total == 0.0
    assert extra.grand_total == pytest.approx(5.30)
    assert extra.grand_total < strict.grand_total


def test_quantity_nets_out_what_you_own():
    req = Requirement(row=2, product_id=1, quantity=max(0, 3 - 1),
                      condition=Condition.NM)
    assert req.quantity == 2


def test_zero_quantity_rows_are_ignored():
    plan = optimize([want(1, 0)], [listing("A", 1.00, product=1)])
    assert plan.orders == []
    assert plan.grand_total == 0.0


# --------------------------------------------------------------------------
# Brute-force cross-check: the strongest evidence the MILP is right
# --------------------------------------------------------------------------

def _brute_force(reqs, listings):
    """Exhaustively price every feasible allocation. Only for tiny instances."""
    options = []
    for ri, req in enumerate(reqs):
        acceptable = [li for li, l in enumerate(listings) if req.accepts(l)]
        combos = []
        # every way to take req.quantity copies from the acceptable listings
        def recurse(idx, left, taken):
            if left == 0:
                combos.append(dict(taken))
                return
            if idx >= len(acceptable):
                return
            li = acceptable[idx]
            for take in range(0, min(left, listings[li].quantity) + 1):
                if take:
                    taken[li] = take
                recurse(idx + 1, left - take, taken)
                taken.pop(li, None)
        recurse(0, req.quantity, {})
        if not combos:
            return None
        options.append(combos)

    best = None
    for choice in itertools.product(*options):
        used = {}
        for combo in choice:
            for li, qty in combo.items():
                used[li] = used.get(li, 0) + qty
        if any(qty > listings[li].quantity for li, qty in used.items()):
            continue
        subtotals, rates = {}, {}
        for li, qty in used.items():
            sid = listings[li].seller_id
            subtotals[sid] = subtotals.get(sid, 0.0) + listings[li].price * qty
            rates[sid] = listings[li].seller_shipping_price
        cost = sum(subtotals.values()) + sum(
            order_shipping(rates[s], sub) for s, sub in subtotals.items()
        )
        if best is None or cost < best - 1e-9:
            best = cost
    return best


@pytest.mark.parametrize("seed", range(60))
def test_milp_matches_brute_force(seed):
    rng = random.Random(seed)
    n_products = rng.randint(1, 3)
    reqs = [want(p, rng.randint(1, 2), row=p) for p in range(1, n_products + 1)]
    # A seller's shipping rate is a property of the seller, not the listing.
    rates = {s: rng.choice([0.0, 0.0, 0.0, 0.99]) for s in "ABC"}
    listings = []
    for product in range(1, n_products + 1):
        for seller in "ABC":
            if rng.random() < 0.35:
                continue
            listings.append(
                listing(
                    seller,
                    round(rng.uniform(0.25, 4.0), 2),
                    qty=rng.randint(1, 3),
                    product=product,
                    rate=rates[seller],
                )
            )

    expected = _brute_force(reqs, listings)
    plan = optimize(reqs, listings)
    if expected is None:
        assert not plan.is_complete
    else:
        assert plan.is_complete
        assert plan.grand_total == pytest.approx(expected, abs=0.011)


@pytest.mark.parametrize("seed", range(40))
def test_milp_matches_brute_force_with_conditions(seed):
    """Same cross-check, but with several rows per product at mixed grades,
    so listings are contested between requirements and condition tiers."""
    rng = random.Random(1000 + seed)
    grades = [Condition.NM, Condition.LP, Condition.MP]
    rates = {s: rng.choice([0.0, 0.0, 0.99]) for s in "AB"}

    reqs, listings, row = [], [], 0
    for product in (1, 2):
        for _ in range(rng.randint(1, 2)):
            row += 1
            reqs.append(
                want(product, rng.randint(1, 2), rng.choice(grades),
                     mode=rng.choice(["min", "min", "exact"]), row=row)
            )
        for seller in "AB":
            for grade in grades:
                if rng.random() < 0.5:
                    continue
                listings.append(
                    listing(seller, round(rng.uniform(0.5, 3.5), 2),
                            qty=rng.randint(1, 2), product=product,
                            condition=grade, rate=rates[seller])
                )

    expected = _brute_force(reqs, listings)
    plan = optimize(reqs, listings)
    if expected is None:
        assert not plan.is_complete
    else:
        assert plan.is_complete
        assert plan.grand_total == pytest.approx(expected, abs=0.011)


def test_plan_totals_are_internally_consistent():
    listings = [
        listing("A", 2.00, qty=2, product=1),
        listing("B", 3.00, qty=2, product=2, rate=0.99),
    ]
    plan = optimize([want(1, 2), want(2, 1)], listings)
    assert plan.items_total == pytest.approx(
        sum(a.cost for o in plan.orders for a in o.allocations)
    )
    assert plan.shipping_total == pytest.approx(sum(o.shipping for o in plan.orders))
    assert plan.grand_total == pytest.approx(plan.items_total + plan.shipping_total)
    for order in plan.orders:
        assert order.total == pytest.approx(order.subtotal + order.shipping)
