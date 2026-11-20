"""Thin client for the public endpoints that power TCGPlayer product pages.

TCGPlayer's catalog/pricing API is not open to new developers, so this reads the
same unauthenticated JSON endpoints the website itself calls. It is deliberately
polite: on-disk response caching, a delay between requests, and backoff on 429s.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from .models import Condition, Listing, condition_from_api

log = logging.getLogger(__name__)

SEARCH_BASE = "https://mp-search-api.tcgplayer.com"
PAGE_SIZE = 50

_DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


class TCGPlayerError(RuntimeError):
    pass


def parse_product_id(value: str) -> int:
    """Extract a numeric product id from a TCGPlayer URL or a bare id."""
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"/product/(\d+)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{4,})\b", text)
    if match:
        return int(match.group(1))
    raise ValueError(f"Could not find a TCGPlayer product id in {value!r}")


def name_from_url(url: str) -> str:
    """Best-effort human name from a product URL slug (used before details load)."""
    match = re.search(r"/product/\d+/([^/?#]+)", str(url))
    if not match:
        return ""
    return match.group(1).replace("-", " ").strip().title()


@dataclass
class ProductInfo:
    product_id: int
    name: str
    set_name: str = ""
    product_line: str = ""
    market_price: float | None = None

    @property
    def display(self) -> str:
        if self.set_name:
            return f"{self.name} ({self.set_name})"
        return self.name


class TCGPlayerClient:
    def __init__(
        self,
        cache_dir: str | Path | None = ".tcg_cache",
        cache_ttl: float = 900.0,
        delay: float = 0.35,
        timeout: float = 30.0,
        max_retries: int = 4,
        session: requests.Session | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)
        self._last_request = 0.0
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- plumbing ---------------------------------------------------------

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.cache_dir / f"{digest}.json"

    def _cache_read(self, key: str) -> Any | None:
        path = self._cache_path(key)
        if not path or not path.exists():
            return None
        if self.cache_ttl >= 0 and (time.time() - path.stat().st_mtime) > self.cache_ttl:
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, key: str, payload: Any) -> None:
        path = self._cache_path(key)
        if not path:
            return
        try:
            path.write_text(json.dumps(payload))
        except OSError:
            log.debug("could not write cache entry %s", path)

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()

    def _request(self, method: str, url: str, body: dict | None = None) -> Any:
        cache_key = f"{method} {url} {json.dumps(body, sort_keys=True) if body else ''}"
        cached = self._cache_read(cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                response = self.session.request(
                    method,
                    url,
                    data=json.dumps(body) if body is not None else None,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise TCGPlayerError(f"Non-JSON response from {url}") from exc
                    self._cache_write(cache_key, payload)
                    return payload
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = TCGPlayerError(
                        f"HTTP {response.status_code} from {url}"
                    )
                else:
                    raise TCGPlayerError(
                        f"HTTP {response.status_code} from {url}: {response.text[:200]}"
                    )
            backoff = (2**attempt) * 0.8 + random.uniform(0, 0.4)
            log.warning("retrying %s in %.1fs (%s)", url, backoff, last_error)
            time.sleep(backoff)

        raise TCGPlayerError(f"Gave up on {url} after {self.max_retries} attempts: {last_error}")

    # -- public API -------------------------------------------------------

    def product_info(self, product_id: int) -> ProductInfo:
        data = self._request("GET", f"{SEARCH_BASE}/v2/product/{product_id}/details")
        return ProductInfo(
            product_id=product_id,
            name=str(data.get("productName") or f"Product {product_id}"),
            set_name=str(data.get("setName") or ""),
            product_line=str(data.get("productLineName") or ""),
            market_price=data.get("marketPrice"),
        )

    def fetch_listings(
        self,
        product_id: int,
        conditions: Iterable[Condition] | None = None,
        printings: Sequence[str] | None = None,
        languages: Sequence[str] = ("English",),
        limit: int | None = 60,
    ) -> list[Listing]:
        """Fetch the cheapest listings for a product.

        ``limit`` caps how many listings (ordered by price + shipping, ascending)
        are pulled per product. ``None`` or 0 fetches every listing.
        """
        term: dict[str, Any] = {"sellerStatus": "Live", "channelId": 0}
        if languages:
            term["language"] = list(languages)
        if conditions:
            term["condition"] = sorted({c.api_name for c in conditions})
        if printings:
            term["printing"] = list(printings)

        collected: list[Listing] = []
        offset = 0
        total = None
        while True:
            page_size = PAGE_SIZE
            if limit:
                page_size = min(PAGE_SIZE, limit - len(collected))
                if page_size <= 0:
                    break
            body = {
                "filters": {
                    "term": term,
                    "range": {"quantity": {"gte": 1}},
                    "exclude": {"channelExclusion": 0},
                },
                "from": offset,
                "size": page_size,
                "sort": {"field": "price+shipping", "order": "asc"},
                "context": {"shippingCountry": "US", "cart": {}},
            }
            payload = self._request(
                "POST", f"{SEARCH_BASE}/v1/product/{product_id}/listings", body
            )
            results = (payload.get("results") or [{}])[0]
            if total is None:
                total = int(results.get("totalResults") or 0)
            rows = results.get("results") or []
            if not rows:
                break
            for row in rows:
                listing = _parse_listing(row, product_id)
                if listing is not None:
                    collected.append(listing)
            offset += len(rows)
            if offset >= (total or 0):
                break
        return collected


def _parse_listing(row: dict, product_id: int) -> Listing | None:
    condition = condition_from_api(row.get("condition", ""))
    if condition is None:
        return None  # e.g. sealed-product conditions like "Unopened"
    try:
        quantity = int(float(row.get("quantity") or 0))
        price = float(row.get("price"))
    except (TypeError, ValueError):
        return None
    if quantity <= 0 or price <= 0:
        return None
    return Listing(
        listing_id=int(float(row.get("listingId") or 0)),
        product_id=int(float(row.get("productId") or product_id)),
        seller_id=str(row.get("sellerId") or ""),
        seller_name=str(row.get("sellerName") or "Unknown seller"),
        price=round(price, 2),
        quantity=quantity,
        condition=condition,
        printing=str(row.get("printing") or ""),
        language=str(row.get("language") or "English"),
        seller_shipping_price=round(float(row.get("sellerShippingPrice") or 0.0), 2),
        shipping_price=round(float(row.get("shippingPrice") or 0.0), 2),
        seller_rating=row.get("sellerRating"),
        seller_sales=str(row.get("sellerSales") or ""),
        direct_seller=bool(row.get("directSeller")),
        gold_seller=bool(row.get("goldSeller")),
        verified_seller=bool(row.get("verifiedSeller")),
        listing_type=str(row.get("listingType") or "standard"),
    )
