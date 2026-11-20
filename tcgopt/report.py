"""Human-readable and machine-readable rendering of a purchase plan."""

from __future__ import annotations

from .models import FREE_SHIPPING_THRESHOLD, Plan


def _money(value: float) -> str:
    return f"${value:,.2f}"


def render_plan(plan: Plan, baseline: Plan | None = None, width: int = 78) -> str:
    lines: list[str] = []
    rule = "=" * width
    thin = "-" * width

    lines.append(rule)
    lines.append("CHEAPEST PURCHASE PLAN".center(width))
    lines.append(rule)

    if not plan.orders:
        lines.append("No listings could be matched to your wants list.")
    for index, order in enumerate(plan.orders, start=1):
        lines.append("")
        lines.append(f"ORDER {index}: {order.seller_name}")
        lines.append(thin)
        for allocation in order.allocations:
            listing = allocation.listing
            name = allocation.requirement.name or f"Product {listing.product_id}"
            detail = f"{listing.condition.label}"
            if listing.printing:
                detail += f"/{listing.printing}"
            left = f"  {allocation.quantity}x {name} [{detail}]"
            right = f"{_money(listing.price)} ea = {_money(allocation.cost)}"
            pad = max(1, width - len(left) - len(right))
            lines.append(f"{left}{' ' * pad}{right}")

        plural = "card" if order.item_count == 1 else "cards"
        subtotal_line = f"  Subtotal ({order.item_count} {plural})"
        lines.append(
            f"{subtotal_line}{' ' * max(1, width - len(subtotal_line) - len(_money(order.subtotal)))}{_money(order.subtotal)}"
        )
        ship_note = "  Shipping"
        if order.shipping == 0:
            ship_note += " (free, order over $5)"
        elif order.seller_rate > 0:
            ship_note += " (seller flat rate)"
        else:
            ship_note += f" (under {_money(FREE_SHIPPING_THRESHOLD)})"
        lines.append(
            f"{ship_note}{' ' * max(1, width - len(ship_note) - len(_money(order.shipping)))}{_money(order.shipping)}"
        )
        total_line = "  Order total"
        lines.append(
            f"{total_line}{' ' * max(1, width - len(total_line) - len(_money(order.total)))}{_money(order.total)}"
        )
        gap = order.free_shipping_gap
        if gap > 0:
            note = f"  note: {_money(gap)} more from this seller unlocks free shipping"
            if order.shipping > gap:
                note += f" (net saving {_money(round(order.shipping - gap, 2))})"
            lines.append(note)

    lines.append("")
    lines.append(rule)
    summary = [
        ("Cards", f"{plan.card_count}"),
        ("Sellers / orders", f"{len(plan.orders)}"),
        ("Items subtotal", _money(plan.items_total)),
        ("Shipping total", _money(plan.shipping_total)),
        ("GRAND TOTAL", _money(plan.grand_total)),
    ]
    for label, value in summary:
        lines.append(f"{label}{' ' * max(1, width - len(label) - len(value))}{value}")
    lines.append(rule)

    if baseline is not None and baseline.orders:
        saving = round(baseline.grand_total - plan.grand_total, 2)
        lines.append("")
        lines.append(
            f"Buying each card from its cheapest seller instead would cost "
            f"{_money(baseline.grand_total)} across {len(baseline.orders)} orders."
        )
        if saving > 0.004:
            pct = saving / baseline.grand_total * 100 if baseline.grand_total else 0
            lines.append(f"This plan saves {_money(saving)} ({pct:.1f}%).")
        else:
            lines.append("The naive plan is already optimal for this list.")

    if plan.unfilled:
        lines.append("")
        lines.append("COULD NOT SOURCE:")
        for requirement, missing in plan.unfilled:
            lines.append(
                f"  {missing}x {requirement.label} - not enough stock at that "
                f"condition/printing"
            )

    lines.append("")
    lines.append(
        "Totals exclude sales tax. Prices move constantly; re-run before buying."
    )
    return "\n".join(lines)


def plan_to_dict(plan: Plan) -> dict:
    return {
        "status": plan.status,
        "grand_total": plan.grand_total,
        "items_total": plan.items_total,
        "shipping_total": plan.shipping_total,
        "card_count": plan.card_count,
        "order_count": len(plan.orders),
        "complete": plan.is_complete,
        "orders": [
            {
                "seller_id": order.seller_id,
                "seller_name": order.seller_name,
                "subtotal": order.subtotal,
                "shipping": order.shipping,
                "total": order.total,
                "free_shipping_gap": order.free_shipping_gap,
                "items": [
                    {
                        "name": allocation.requirement.name,
                        "product_id": allocation.listing.product_id,
                        "url": allocation.requirement.url,
                        "quantity": allocation.quantity,
                        "unit_price": allocation.listing.price,
                        "cost": allocation.cost,
                        "condition": allocation.listing.condition.label,
                        "printing": allocation.listing.printing,
                        "language": allocation.listing.language,
                        "listing_id": allocation.listing.listing_id,
                        "wants_row": allocation.requirement.row,
                    }
                    for allocation in order.allocations
                ],
            }
            for order in plan.orders
        ],
        "unfilled": [
            {
                "name": requirement.name,
                "product_id": requirement.product_id,
                "wants_row": requirement.row,
                "missing": missing,
                "condition": requirement.condition.label,
            }
            for requirement, missing in plan.unfilled
        ],
    }
