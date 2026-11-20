"""Command line entry point: wants CSV in, cheapest multi-seller basket out."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .client import TCGPlayerClient, TCGPlayerError
from .optimizer import naive_plan, optimize
from .planner import gather_listings, usable_listings
from .report import plan_to_dict, render_plan
from .wants import EXAMPLE_CSV, WantsError, parse_wants


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tcgopt",
        description=(
            "Find the cheapest way to buy a list of TCGPlayer cards, including "
            "per-seller shipping."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "wants CSV columns:\n"
            "  url           TCGPlayer product link or bare product id   (required)\n"
            "  needed        how many copies you want                    (required)\n"
            "  has           how many you already own        (optional, default 0)\n"
            "  condition     NM | LP | MP | HP | DMG        (optional, default NM)\n"
            "  condition_mode  min = that condition or better (default), or exact\n"
            "  name          label for the report                       (optional)\n"
            "  printing      e.g. Holofoil / Normal; blank = any        (optional)\n"
            "  language      default English                            (optional)\n"
        ),
    )
    parser.add_argument("wants", nargs="?", help="path to the wants CSV")
    parser.add_argument(
        "--max-sellers", type=int, default=None,
        help="cap how many separate orders/sellers the plan may use",
    )
    parser.add_argument(
        "--allow-extra", action="store_true",
        help="allow buying spare copies when that is cheaper (pads an order "
             "over the $5 free-shipping line)",
    )
    parser.add_argument(
        "--listings-per-card", type=int, default=60,
        help="how many of the cheapest listings to consider per card "
             "(0 = all listings; higher is more thorough but slower)",
    )
    parser.add_argument(
        "--max-condition-fetch", action="store_true",
        help="fetch every condition and filter locally instead of asking the "
             "API for only the conditions you accept",
    )
    parser.add_argument("--json", dest="json_out", help="also write the plan as JSON")
    parser.add_argument("--time-limit", type=float, default=60.0,
                        help="solver time limit in seconds (default 60)")
    parser.add_argument("--cache-dir", default=".tcg_cache",
                        help="where to cache API responses (default .tcg_cache)")
    parser.add_argument("--cache-ttl", type=float, default=900.0,
                        help="seconds before cached prices are refetched (default 900)")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached responses and refetch everything")
    parser.add_argument("--no-cache", action="store_true", help="disable caching entirely")
    parser.add_argument("--delay", type=float, default=0.35,
                        help="seconds between API requests (default 0.35)")
    parser.add_argument("--no-baseline", action="store_true",
                        help="skip the cheapest-per-card comparison")
    parser.add_argument("--init", metavar="PATH", nargs="?", const="wants.csv",
                        help="write an example wants CSV and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser



def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    if args.init is not None:
        path = Path(args.init)
        if path.exists():
            print(f"Refusing to overwrite existing {path}", file=sys.stderr)
            return 1
        path.write_text(EXAMPLE_CSV)
        print(f"Wrote example wants list to {path}")
        return 0

    if not args.wants:
        build_parser().print_help()
        return 1

    try:
        requirements = parse_wants(args.wants)
    except WantsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    to_buy = [r for r in requirements if r.quantity > 0]
    if not to_buy:
        print("Nothing to buy - every row is already covered by what you own.")
        return 0

    total_cards = sum(r.quantity for r in to_buy)
    print(
        f"Need {total_cards} cards across {len(to_buy)} rows "
        f"({len({r.product_id for r in to_buy})} distinct products).",
        file=sys.stderr,
    )

    client = TCGPlayerClient(
        cache_dir=None if args.no_cache else args.cache_dir,
        # ttl 0 expires every entry immediately, so --refresh refetches but
        # still repopulates the cache for subsequent runs.
        cache_ttl=0.0 if args.refresh else args.cache_ttl,
        delay=args.delay,
    )

    def show_progress(step: int, total: int, label: str) -> None:
        print(f"  [{step}/{total}] fetching listings for {label} ...", file=sys.stderr)

    try:
        listings = gather_listings(
            client,
            to_buy,
            listings_per_card=args.listings_per_card or None,
            filter_conditions=not args.max_condition_fetch,
            progress=show_progress,
        )
    except TCGPlayerError as exc:
        print(f"error talking to TCGPlayer: {exc}", file=sys.stderr)
        return 2

    usable = usable_listings(listings, to_buy)
    print(
        f"Considering {len(usable)} listings from "
        f"{len({l.seller_id for l in usable})} sellers.\n",
        file=sys.stderr,
    )

    plan = optimize(
        to_buy,
        usable,
        max_sellers=args.max_sellers,
        allow_extra=args.allow_extra,
        time_limit=args.time_limit,
    )
    baseline = None if args.no_baseline else naive_plan(to_buy, usable)

    print(render_plan(plan, baseline))

    if args.json_out:
        payload = plan_to_dict(plan)
        if baseline is not None:
            payload["baseline_total"] = baseline.grand_total
            payload["saving_vs_baseline"] = round(
                baseline.grand_total - plan.grand_total, 2
            )
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"\nWrote {args.json_out}", file=sys.stderr)

    return 0 if plan.is_complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
