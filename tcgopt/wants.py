"""Parsing of the wants list (the spreadsheet you fill in)."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .client import name_from_url, parse_product_id
from .models import Condition, Requirement

# Accepted column spellings -> canonical field.
_ALIASES = {
    "url": "url",
    "link": "url",
    "product": "url",
    "productid": "url",
    "product_id": "url",
    "id": "url",
    "name": "name",
    "card": "name",
    "cardname": "name",
    "needed": "needed",
    "need": "needed",
    "qty": "needed",
    "quantity": "needed",
    "want": "needed",
    "has": "has",
    "have": "has",
    "owned": "has",
    "condition": "condition",
    "cond": "condition",
    "conditionmode": "condition_mode",
    "condition_mode": "condition_mode",
    "mode": "condition_mode",
    "printing": "printing",
    "variant": "printing",
    "finish": "printing",
    "foil": "printing",
    "language": "language",
    "lang": "language",
}


class WantsError(ValueError):
    pass


def _normalise(header: str) -> str:
    return re.sub(r"[^a-z_]", "", str(header or "").strip().lower())


def _as_int(value: str, field: str, row_no: int, default: int | None = None) -> int:
    text = str(value or "").strip()
    if not text:
        if default is not None:
            return default
        raise WantsError(f"Row {row_no}: missing required column '{field}'")
    try:
        return int(float(text))
    except ValueError:
        raise WantsError(f"Row {row_no}: '{field}' must be a whole number, got {value!r}") from None


def parse_wants(source: str | Path | io.TextIOBase) -> list[Requirement]:
    """Read a wants CSV into Requirements (quantity = needed - has).

    Required columns: url (or product_id) and needed.
    Optional: name, has, condition (default NM), condition_mode (min|exact),
    printing, language (default English).
    """
    if isinstance(source, (str, Path)) and not isinstance(source, io.TextIOBase):
        path = Path(source)
        if not path.exists():
            raise WantsError(f"Wants file not found: {path}")
        text = path.read_text(encoding="utf-8-sig")
        handle: Iterable[str] = io.StringIO(text)
    else:
        handle = source

    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        raise WantsError("Wants file is empty or has no header row")

    mapping: dict[str, str] = {}
    for header in reader.fieldnames:
        canonical = _ALIASES.get(_normalise(header))
        if canonical:
            mapping[header] = canonical

    if "url" not in mapping.values():
        raise WantsError(
            "Wants file needs a 'url' column (a TCGPlayer product link or product id). "
            f"Found columns: {reader.fieldnames}"
        )
    if "needed" not in mapping.values():
        raise WantsError(
            f"Wants file needs a 'needed' column. Found columns: {reader.fieldnames}"
        )

    requirements: list[Requirement] = []
    for offset, raw in enumerate(reader, start=2):
        row = {canonical: raw.get(header) for header, canonical in mapping.items()}
        if not any(str(v or "").strip() for v in row.values()):
            continue  # blank line

        url = str(row.get("url") or "").strip()
        if not url:
            raise WantsError(f"Row {offset}: missing product url/id")
        try:
            product_id = parse_product_id(url)
        except ValueError as exc:
            raise WantsError(f"Row {offset}: {exc}") from None

        needed = _as_int(row.get("needed"), "needed", offset)
        has = _as_int(row.get("has"), "has", offset, default=0)
        if needed < 0 or has < 0:
            raise WantsError(f"Row {offset}: quantities cannot be negative")

        condition_raw = str(row.get("condition") or "NM").strip() or "NM"
        try:
            condition = Condition.parse(condition_raw)
        except ValueError as exc:
            raise WantsError(f"Row {offset}: {exc}") from None

        mode = str(row.get("condition_mode") or "min").strip().lower() or "min"
        if mode in ("minimum", "or better", "orbetter", "atleast"):
            mode = "min"
        if mode not in ("min", "exact"):
            raise WantsError(
                f"Row {offset}: condition_mode must be 'min' or 'exact', got {mode!r}"
            )

        printing = str(row.get("printing") or "").strip() or None
        language = str(row.get("language") or "English").strip() or "English"
        name = str(row.get("name") or "").strip() or name_from_url(url)

        requirements.append(
            Requirement(
                row=offset,
                product_id=product_id,
                quantity=max(0, needed - has),
                condition=condition,
                condition_mode=mode,
                printing=printing,
                language=language,
                name=name,
                url=url,
                needed=needed,
                has=has,
            )
        )

    if not requirements:
        raise WantsError("No rows found in wants file")
    return requirements


EXAMPLE_CSV = """url,name,needed,has,condition,condition_mode,printing,language
https://www.tcgplayer.com/product/722583/lorcana-tcg-hyperia-city-pepita-imeldas-right-hand-enchanted,Pepita - Imelda's Right Hand,1,0,NM,min,,English
"""
