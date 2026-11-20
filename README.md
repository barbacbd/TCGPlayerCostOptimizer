# TCG Cost Optimizer — cheapest-basket optimizer for TCGPlayer

You need N copies of a bunch of cards. TCGPlayer charges shipping **per seller,
per order**, and waives it once a seller's subtotal hits **$5.00**. So the
cheapest price on each individual card is very often *not* the cheapest way to
buy the list: paying a few cents more per card to collapse five orders into two,
or to push one order over the free-shipping line, routinely wins.

This tool reads a spreadsheet of what you need, pulls live listings for each
card, and solves for the genuinely cheapest combination of sellers.

On a real 12-card list it found a basket **73.7% cheaper** than buying each card
from its lowest-priced seller ($2.07 vs $7.86), by consolidating 5 orders into 1.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

There are two front ends: a desktop window and a command line. Both drive the
same fetcher and solver.

## Desktop app

```bash
.venv/bin/python -m tcgopt.gui
```

Tkinter, so no extra dependencies — it ships with Python. (If you `pip install
-e .`, the same window is also available as the `tcgopt-gui` command.)

Paste a TCGPlayer product link, set **Need** / **Have** / **Condition**, and
click **Add to list**; Return in the link box does the same. The card's real
name is fetched in the background, so a row briefly shows `Loading name...`
before filling itself in. Double-click a row to open its TCGPlayer page.
Condition means *that grade or better* unless you tick **exact grade only** —
and as on the CLI, two grades of one card means adding it twice.

**Find cheapest plan** fetches listings and solves on a background thread, so
the window stays responsive and the status line reports progress per card. The
finished report is the same text the CLI prints, with buttons to copy it to the
clipboard or save it to a file.

Three controls sit next to the button, mirroring the CLI flags:

| Control | CLI equivalent |
| --- | --- |
| **Max orders** (blank = no limit) | `--max-sellers N` |
| **buy spares if cheaper** | `--allow-extra` |
| **thorough search (slower)** | `--listings-per-card 0` (all listings instead of the cheapest 60) |

**Save list...** writes an ordinary wants CSV and **Load list...** reads one, so
the GUI and the CLI share list files in both directions — build a list in the
window, then re-run it from a script later. Everything else uses defaults: the
60-second solver time limit, and the 15-minute price cache (the GUI has no
equivalent of `--refresh`, so to force fresh prices run the CLI once with it, or
delete `.tcg_cache/`).

## Command line

Create your wants list (`--init` writes a template):

```bash
.venv/bin/python -m tcgopt.cli --init wants.csv
```

```csv
url,name,needed,has,condition,condition_mode,printing,language
https://www.tcgplayer.com/product/722583/...pepita...,Pepita,3,1,NM,min,,English
https://www.tcgplayer.com/product/722500/,Hiro Hamada,4,0,NM,,,
https://www.tcgplayer.com/product/722540/,Sir Ector,6,0,MP,,,
```

```bash
.venv/bin/python -m tcgopt.cli wants.csv
```

```
ORDER 1: BlueSideGaming
------------------------------------------------------------------------------
  1x Pepita [NM/Holofoil]                                   $59.54 ea = $59.54
  Subtotal (1 card)                                                     $59.54
  Shipping (seller flat rate)                                            $0.99
  Order total                                                           $60.53

ORDER 3: LSPulls
------------------------------------------------------------------------------
  4x Hiro Hamada [NM/Normal]                                  $0.03 ea = $0.12
  2x Institute of Technology [NM/Normal]                      $0.05 ea = $0.10
  6x Sir Ector [NM/Normal]                                    $0.06 ea = $0.36
  Subtotal (12 cards)                                                    $0.58
  Shipping (under $5.00)                                                 $1.49
  Order total                                                            $2.07
  note: $4.42 more from this seller unlocks free shipping

==============================================================================
GRAND TOTAL                                                            $122.60
==============================================================================
Buying each card from its cheapest seller instead would cost $141.29 across 7 orders.
This plan saves $18.69 (13.2%).
```

### Wants list columns

| Column | Required | Meaning |
| --- | --- | --- |
| `url` | yes | TCGPlayer product link, or a bare product id. Aliases: `link`, `product_id` |
| `needed` | yes | How many copies you want. Aliases: `qty`, `quantity`, `need` |
| `has` | no | How many you already own (default 0). The tool buys `needed - has` |
| `condition` | no | `NM`, `LP`, `MP`, `HP`, `DMG` (or full names). Default `NM` |
| `condition_mode` | no | `min` (default) = that grade **or better**; `exact` = only that grade |
| `name` | no | Label for the report; auto-filled from TCGPlayer if blank |
| `printing` | no | e.g. `Holofoil`, `Normal`. Blank = any printing (cheapest wins) |
| `language` | no | Default `English` |

**Two conditions of the same card = two rows.** With the default `min` mode a
row asking for `LP` is happily filled with NM copies if they're cheaper, and
both rows draw from the same shared stock without double-counting. Use `exact`
when you specifically want that grade and no other.

### Options

| Flag | Effect |
| --- | --- |
| `--max-sellers N` | Cap the number of separate orders (fewer packages, slightly higher price) |
| `--allow-extra` | Allow buying *spare* copies when cheaper — e.g. a $0.40 extra card to clear the $5 free-shipping line and save $1.49 |
| `--listings-per-card N` | Candidate listings pulled per card (default 60; `0` = all) |
| `--json out.json` | Machine-readable plan |
| `--refresh` / `--no-cache` | Bypass the on-disk price cache |
| `--max-condition-fetch` | Fetch all conditions and filter locally |
| `--time-limit S` | Solver time limit (default 60s) |

Exit code is `0` on a complete plan, `3` if some copies could not be sourced
(those are listed under `COULD NOT SOURCE`), `1`/`2` on input or network errors.

## How shipping is modelled

The shipping rule was derived empirically by sampling **1,098 live listings**
across six products and fitting a rule to the observed buyer-facing prices. It
matched **1098/1098**:

```
shipping(order) = seller_rate                 if seller subtotal >= $5.00
                  max(seller_rate, $1.49)     if seller subtotal <  $5.00
```

where `seller_rate` is the listing's `sellerShippingPrice` — `0.00` for the
majority of sellers (who participate in TCGPlayer's free-shipping-over-$5
program) and a flat per-order fee for the rest. Observed boundaries: sellers
with rate 0 charged $1.49 at prices up to $4.99 and exactly $0.00 from $5.00
upward; flat-rate sellers charged their rate even on a $249.99 card, but never
less than the $1.49 floor on cheap ones.

The constants live in `tcgopt/models.py` (`FREE_SHIPPING_THRESHOLD`,
`MIN_SHIPPING`) if TCGPlayer changes the program.

## How the optimization works

Shipping being a *fixed cost per seller* with a *threshold discount* makes this
a capacitated facility-location problem, not something a greedy pass solves
correctly. It's formulated as a MILP and solved exactly with CBC (bundled with
PuLP):

- `x[listing, row]` — integer copies of a listing bought for a wants row.
- `y[seller]` — binary, whether you order from that seller at all.
- `free[seller]` — binary, whether that order clears the $5 threshold;
  constrained by `5·free ≤ subtotal`, so it can only be 1 when truly earned.
- Objective: `Σ price·x + Σ (rate·y + (max(rate,1.49) − rate)·(y − free))`.

Shortfalls use a penalised slack variable, so an under-supplied list still
returns the best partial plan instead of failing to solve.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q     # 137 tests
```

The suite runs offline on synthetic listings. The strongest check is 100
randomised instances cross-verified against a brute-force enumeration of every
feasible allocation — including cases where several rows of different
conditions contend for the same stock. It's what caught the two real bugs in
the first draft.

## Caveats worth knowing

- **Not an official API.** TCGPlayer's developer API is closed to new
  applications, so this reads the same unauthenticated JSON endpoints the
  product pages use. It caches responses (15 min default) and rate-limits
  itself. If the site changes shape, `tcgopt/client.py` is where it breaks.
  Keep your volume reasonable and review TCGPlayer's terms for your own use.
- **Sales tax is excluded** — it varies by state and seller.
- **`--listings-per-card` is a real tradeoff.** The default considers the 60
  cheapest listings per card. A seller ranked #61 on one card who could have
  consolidated your whole order won't be found. Raise it, or pass `0`, for a
  thorough (slower) search on expensive lists.
- **Prices move constantly.** Re-run immediately before buying; a plan built on
  15-minute-old prices can already be stale.
- The plan is a shopping list, not a cart — you still place the orders
  yourself, one per seller shown.
