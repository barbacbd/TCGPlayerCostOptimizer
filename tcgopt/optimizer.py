"""Minimum-total-cost purchase planning across TCGPlayer sellers.

The problem is a capacitated facility-location variant: every seller you order
from adds a fixed shipping charge, which is waived once that seller's subtotal
reaches $5.00. So the cheapest per-card prices are frequently *not* the cheapest
basket -- paying a few cents more per card to consolidate into fewer orders, or
to push one order over the free-shipping line, often wins.

This is solved exactly as a MILP with CBC (bundled with PuLP) rather than
greedily, because the shipping threshold makes greedy choices provably wrong in
common cases (see tests/test_optimizer.py::test_beats_greedy_on_threshold).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable, Sequence

import pulp

from .models import (
    FREE_SHIPPING_THRESHOLD,
    MIN_SHIPPING,
    Allocation,
    Listing,
    Plan,
    Requirement,
    SellerOrder,
    SellerTerms,
    order_shipping,
)

log = logging.getLogger(__name__)

# Cost charged per copy we fail to source, so the model stays feasible and
# reports shortfalls instead of refusing to solve. Far above any real card price.
UNFILLED_PENALTY = 1_000_000.0


def seller_terms(listings: Iterable[Listing]) -> dict[str, SellerTerms]:
    """Collapse listings into per-seller shipping terms."""
    terms: dict[str, SellerTerms] = {}
    for listing in listings:
        existing = terms.get(listing.seller_id)
        # A seller's rate should be constant; take the max if it ever differs
        # so we never under-estimate the bill.
        rate = listing.seller_shipping_price
        if existing is None or rate > existing.rate:
            terms[listing.seller_id] = SellerTerms(
                seller_id=listing.seller_id,
                seller_name=listing.seller_name,
                rate=rate,
            )
    return terms


def optimize(
    requirements: Sequence[Requirement],
    listings: Sequence[Listing],
    *,
    max_sellers: int | None = None,
    allow_extra: bool = False,
    threshold: float = FREE_SHIPPING_THRESHOLD,
    floor: float = MIN_SHIPPING,
    time_limit: float | None = 60.0,
    msg: bool = False,
) -> Plan:
    """Return the cheapest purchase plan covering ``requirements``.

    Args:
        requirements: what to buy (quantities already net of what you own).
        listings: candidate offers, typically from TCGPlayerClient.
        max_sellers: cap the number of distinct sellers/orders.
        allow_extra: permit buying *more* copies than requested when doing so
            is cheaper (i.e. padding an order over the free-shipping threshold).
        threshold / floor: shipping model parameters.
        time_limit: CBC time limit in seconds.
    """
    wanted = [r for r in requirements if r.quantity > 0]
    if not wanted:
        return Plan(status="Optimal")

    terms = seller_terms(listings)
    by_seller: dict[str, list[int]] = defaultdict(list)
    for index, listing in enumerate(listings):
        by_seller[listing.seller_id].append(index)

    problem = pulp.LpProblem("tcg_basket", pulp.LpMinimize)

    # x[listing, requirement] = copies of that listing bought for that row.
    # Index the variables as we create them; scanning the whole dict per
    # seller/row turns model building quadratic on realistic wants lists.
    x: dict[tuple[int, int], pulp.LpVariable] = {}
    vars_by_req: dict[int, list[pulp.LpVariable]] = defaultdict(list)
    vars_by_listing: dict[int, list[pulp.LpVariable]] = defaultdict(list)
    vars_by_seller: dict[str, list[tuple[int, pulp.LpVariable]]] = defaultdict(list)
    for li, listing in enumerate(listings):
        for ri, requirement in enumerate(wanted):
            if not requirement.accepts(listing):
                continue
            upper = listing.quantity if allow_extra else min(
                listing.quantity, requirement.quantity
            )
            if upper <= 0:
                continue
            var = pulp.LpVariable(
                f"x_{li}_{ri}", lowBound=0, upBound=upper, cat="Integer"
            )
            x[(li, ri)] = var
            vars_by_req[ri].append(var)
            vars_by_listing[li].append(var)
            vars_by_seller[listing.seller_id].append((li, var))

    # Slack: copies we simply cannot source.
    short = {
        ri: pulp.LpVariable(f"short_{ri}", lowBound=0, upBound=r.quantity, cat="Integer")
        for ri, r in enumerate(wanted)
    }

    # y[seller] = 1 if we place an order with that seller.
    # free[seller] = 1 if that order clears the free-shipping threshold.
    y = {sid: pulp.LpVariable(f"y_{i}", cat="Binary") for i, sid in enumerate(terms)}
    free = {sid: pulp.LpVariable(f"f_{i}", cat="Binary") for i, sid in enumerate(terms)}

    # --- Coverage: every requested copy is bought or explicitly unfilled.
    for ri, requirement in enumerate(wanted):
        supplied = pulp.lpSum(vars_by_req[ri])
        if allow_extra:
            problem += supplied + short[ri] >= requirement.quantity, f"cover_{ri}"
        else:
            problem += supplied + short[ri] == requirement.quantity, f"cover_{ri}"

    # --- Stock: cannot buy more copies of a listing than the seller has.
    for li, listing in enumerate(listings):
        if vars_by_listing[li]:
            problem += (
                pulp.lpSum(vars_by_listing[li]) <= listing.quantity,
                f"stock_{li}",
            )

    # --- Linking + shipping, per seller.
    shipping_cost = []
    for sid, term in terms.items():
        entries = vars_by_seller.get(sid)
        if not entries:
            problem += y[sid] == 0, f"unused_{sid}"
            problem += free[sid] == 0, f"unused_free_{sid}"
            continue

        capacity = sum(listings[li].quantity for li in by_seller[sid])
        problem += (
            pulp.lpSum(var for _, var in entries) <= capacity * y[sid],
            f"open_{sid}",
        )

        subtotal = pulp.lpSum(listings[li].price * var for li, var in entries)
        # free[sid] may only be 1 once the subtotal reaches the threshold.
        problem += threshold * free[sid] <= subtotal, f"thresh_{sid}"
        problem += free[sid] <= y[sid], f"free_implies_open_{sid}"

        # shipping = rate if the order is free-shipping-eligible,
        #            max(rate, floor) otherwise.
        delta = max(term.rate, floor) - term.rate  # >= 0
        shipping_cost.append(term.rate * y[sid] + delta * (y[sid] - free[sid]))

    if max_sellers is not None:
        problem += pulp.lpSum(y.values()) <= max_sellers, "max_sellers"

    items_cost = pulp.lpSum(listings[li].price * var for (li, _), var in x.items())
    penalty = pulp.lpSum(UNFILLED_PENALTY * v for v in short.values())
    problem += items_cost + pulp.lpSum(shipping_cost) + penalty

    solver = pulp.PULP_CBC_CMD(msg=bool(msg), timeLimit=time_limit)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]

    if status not in ("Optimal", "Not Solved"):
        return Plan(status=status, unfilled=[(r, r.quantity) for r in wanted])

    return _build_plan(wanted, listings, terms, x, short, status, threshold, floor)


def _build_plan(
    wanted: Sequence[Requirement],
    listings: Sequence[Listing],
    terms: dict[str, SellerTerms],
    x: dict[tuple[int, int], pulp.LpVariable],
    short: dict[int, pulp.LpVariable],
    status: str,
    threshold: float,
    floor: float,
) -> Plan:
    orders: dict[str, SellerOrder] = {}
    for (li, ri), var in x.items():
        qty = int(round(var.value() or 0))
        if qty <= 0:
            continue
        listing = listings[li]
        order = orders.get(listing.seller_id)
        if order is None:
            term = terms[listing.seller_id]
            order = SellerOrder(
                seller_id=listing.seller_id,
                seller_name=term.seller_name,
                seller_rate=term.rate,
            )
            orders[listing.seller_id] = order
        order.allocations.append(
            Allocation(listing=listing, requirement=wanted[ri], quantity=qty)
        )

    for order in orders.values():
        order.allocations.sort(key=lambda a: (a.requirement.row, -a.listing.price))
        order.shipping = order_shipping(order.seller_rate, order.subtotal, threshold, floor)

    unfilled = []
    for ri, requirement in enumerate(wanted):
        missing = int(round(short[ri].value() or 0))
        if missing > 0:
            unfilled.append((requirement, missing))

    plan = Plan(
        orders=sorted(orders.values(), key=lambda o: -o.total),
        unfilled=unfilled,
        status=status,
    )
    return plan


def naive_plan(
    requirements: Sequence[Requirement],
    listings: Sequence[Listing],
    *,
    threshold: float = FREE_SHIPPING_THRESHOLD,
    floor: float = MIN_SHIPPING,
) -> Plan:
    """Baseline: fill each row from the cheapest listings, ignoring consolidation.

    This models what you'd do clicking through the site by hand -- buy the
    lowest price per card -- and exists to quantify what the optimizer saves.
    """
    wanted = [r for r in requirements if r.quantity > 0]
    terms = seller_terms(listings)
    remaining: dict[int, int] = {i: l.quantity for i, l in enumerate(listings)}
    orders: dict[str, SellerOrder] = {}
    unfilled: list[tuple[Requirement, int]] = []

    for requirement in wanted:
        need = requirement.quantity
        candidates = sorted(
            (i for i, l in enumerate(listings) if requirement.accepts(l)),
            key=lambda i: (listings[i].price, listings[i].shipping_price),
        )
        for index in candidates:
            if need <= 0:
                break
            take = min(need, remaining[index])
            if take <= 0:
                continue
            listing = listings[index]
            remaining[index] -= take
            need -= take
            order = orders.get(listing.seller_id)
            if order is None:
                term = terms[listing.seller_id]
                order = SellerOrder(
                    seller_id=listing.seller_id,
                    seller_name=term.seller_name,
                    seller_rate=term.rate,
                )
                orders[listing.seller_id] = order
            order.allocations.append(
                Allocation(listing=listing, requirement=requirement, quantity=take)
            )
        if need > 0:
            unfilled.append((requirement, need))

    for order in orders.values():
        order.shipping = order_shipping(order.seller_rate, order.subtotal, threshold, floor)

    return Plan(
        orders=sorted(orders.values(), key=lambda o: -o.total),
        unfilled=unfilled,
        status="Naive",
    )
