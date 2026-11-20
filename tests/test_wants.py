"""Tests for wants-list parsing, product id extraction and listing parsing."""

from __future__ import annotations

import io

import pytest

from tcgopt.client import _parse_listing, name_from_url, parse_product_id
from tcgopt.models import Condition
from tcgopt.wants import WantsError, parse_wants

PEPITA = (
    "https://www.tcgplayer.com/product/722583/lorcana-tcg-hyperia-city-pepita-"
    "imeldas-right-hand-enchanted?page=1&Language=English"
)


def parse(text: str):
    return parse_wants(io.StringIO(text))


def test_parses_the_users_example():
    rows = parse(
        "url,needed,has,condition\n"
        f"{PEPITA},4,0,NM\n"
        f"{PEPITA},3,1,LP\n"
        f"{PEPITA},6,0,MP\n"
    )
    assert [r.quantity for r in rows] == [4, 2, 6]
    assert [r.condition for r in rows] == [Condition.NM, Condition.LP, Condition.MP]
    assert all(r.product_id == 722583 for r in rows)


def test_product_id_from_url_and_bare_id():
    assert parse_product_id(PEPITA) == 722583
    assert parse_product_id("722583") == 722583
    assert parse_product_id("https://www.tcgplayer.com/product/722583/") == 722583
    with pytest.raises(ValueError):
        parse_product_id("not-a-product")


def test_name_from_url_slug():
    assert "Pepita" in name_from_url(PEPITA)


def test_column_aliases_and_defaults():
    rows = parse("Link,Quantity,Owned,Cond\n722583,2,1,lightly played\n")
    assert rows[0].quantity == 1
    assert rows[0].condition is Condition.LP
    assert rows[0].condition_mode == "min"
    assert rows[0].language == "English"


def test_condition_defaults_to_nm():
    rows = parse("url,needed\n722583,1\n")
    assert rows[0].condition is Condition.NM


def test_exact_mode_parsed():
    rows = parse("url,needed,condition,condition_mode\n722583,1,LP,exact\n")
    assert rows[0].condition_mode == "exact"


def test_has_greater_than_needed_clamps_to_zero():
    rows = parse("url,needed,has\n722583,2,5\n")
    assert rows[0].quantity == 0


def test_blank_lines_skipped():
    rows = parse("url,needed\n722583,1\n\n,\n")
    assert len(rows) == 1


def test_missing_required_columns():
    with pytest.raises(WantsError, match="needed"):
        parse("url\n722583\n")
    with pytest.raises(WantsError, match="url"):
        parse("needed\n3\n")


def test_bad_values_raise_with_row_numbers():
    with pytest.raises(WantsError, match="Row 2"):
        parse("url,needed\n722583,many\n")
    with pytest.raises(WantsError, match="Row 2"):
        parse("url,needed,condition\n722583,1,PERFECT\n")
    with pytest.raises(WantsError, match="negative"):
        parse("url,needed\n722583,-1\n")


def test_parse_listing_from_api_shape():
    """Shape mirrors a real response row from the live listings endpoint."""
    row = {
        "listingId": 926653566.0,
        "productId": 722583.0,
        "sellerId": "26015",
        "sellerName": "Top Cut Chicago",
        "price": 60.0,
        "quantity": 2.0,
        "condition": "Near Mint",
        "printing": "Holofoil",
        "language": "English",
        "sellerShippingPrice": 0.0,
        "shippingPrice": 0.0,
        "sellerRating": 99.9,
        "directSeller": False,
        "goldSeller": True,
    }
    listing = _parse_listing(row, 722583)
    assert listing.price == 60.0
    assert listing.quantity == 2
    assert listing.condition is Condition.NM
    assert listing.seller_name == "Top Cut Chicago"
    assert listing.solo_total == 60.0


def test_parse_listing_skips_sealed_and_empty_stock():
    base = {
        "listingId": 1, "productId": 1, "sellerId": "x", "sellerName": "x",
        "price": 5.0, "quantity": 1.0, "printing": "", "language": "English",
        "sellerShippingPrice": 0.0, "shippingPrice": 0.0,
    }
    assert _parse_listing({**base, "condition": "Unopened"}, 1) is None
    assert _parse_listing({**base, "condition": "Near Mint", "quantity": 0}, 1) is None
