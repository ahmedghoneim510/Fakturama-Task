# Test corpus

`order_001.png` is the image supplied with the brief. The other three were added
during development, because tuning an extractor and a UI flow against a single
document teaches you very little about either — the first order that differs in
shape is where the real defects surface, and two of the four below were added
*after* a bug they would have caught.

All data is synthetic. No real person, company, address, or payment detail appears
in any of these files.

## What each one covers

| Image | Order | Why it's here |
|---|---|---|
| `order_001.png` | `WEB-2026-0714-A17`<br>Northstar Office GmbH | The brief's own sample. 2 lines, 19% VAT, one line discounted, `Bank Transfer` · **PAID**. Its delivery address differs from billing — the branch the brief describes only for the *identical* case, and therefore the one it is easiest to not implement. |
| `order_apex_vat14.png` | `WEB-2026-0829-C88`<br>Apex Innovations GmbH | **14% VAT** — an uncommon rate. Whether it already exists in the target install is exactly what decides reuse-vs-create, and the brief requires the VAT be resolved *before* the product so the rate is available in the product editor's dropdown. Both lines discounted (10%, 5%). |
| `order_global-logistics_4lines.png` | `WEB-2027-0105-C34`<br>Global Logistics Services SAS | **4 item lines** where the sample has 2 — the per-item loop stops being trivially correct. France, **20% VAT**, non-German addresses, quantities up to 25. Payment method is **`ACH Transfer`, which has no entry in `config/app.yaml`'s `payment_code_map`** — so a correct run must halt for manual review rather than guess a payment code. |
| `order_eurotech.png` | `WEB-2026-0819-B22`<br>EuroTech Solutions GmbH | The document behind `completeness_gaps()` in `src/fic/extraction.py`. One model read the contact as `Elena Richter`; another returned `first_name=None, last_name=None` while still returning her email. The extraction was *structurally valid*, so nothing complained — and the damage only appeared later as an unmatchable debtor and a duplicate contact. The completeness pass and the automatic re-read exist because of this image. |

## Coverage matrix

| | 001 | apex | global-logistics | eurotech |
|---|:--:|:--:|:--:|:--:|
| Line count | 2 | 2 | **4** | 2 |
| VAT rate | 19% | **14%** | **20%** | 19% |
| Line discounts | one | both | three of four | both |
| Delivery ≠ billing | ✅ | ✅ | ✅ | ✅ |
| Payment status | PAID | PAID | PAID | PAID |
| Payment method mapped? | ✅ | ✅ | **❌ halts** | ✅ |
| Non-German addresses | | | ✅ | |

The **UNPAID** path is covered by `../golden/order_002.json` (Greenfield Technologies,
`WEB-2026-0825-C19`) rather than by an image — brief §5.3 requires that an unpaid
invoice leave `paid` clear and invent neither a date nor a value, and that branch is
reachable without spending an API call.

## All four reconcile

Each image's printed line totals, net, VAT and gross agree with the values recomputed
independently from quantity × unit price × (1 − discount), so any tier-2
`reconcile()` failure on these inputs indicts the extraction, never the document:

| Order | Line nets | Net | VAT | Gross |
|---|---|---|---|---|
| `order_001` | 450.00 · 120.00 | 570.00 | 108.30 | 678.30 |
| `order_apex_vat14` | 1080.00 · 1045.00 | 2125.00 | 297.50 | 2422.50 |
| `order_global-logistics_4lines` | 8075.00 · 3150.00 · 3100.00 · 2176.20 | 16501.20 | 3300.24 | 19801.44 |
| `order_eurotech` | 255.00 · 342.00 | 597.00 | 113.43 | 710.43 |

## Running them

```bash
# extraction only — no UI, no Fakturama needed (needs an API key)
uv run fic extract data/input/order_apex_vat14.png -o out.json

# extract + reconcile, then stop before touching the UI
uv run fic run data/input/order_global-logistics_4lines.png --dry-run

# full flow: image -> saved, verified Order + linked Invoice
uv run fic run data/input/order_001.png

# no API key: run the flow from an already-extracted fixture
uv run fic run --from-json data/golden/order_002.json
```

Extracted fixtures live in [`../golden/`](../golden). `--from-json` replaces only the
extraction step — the payload still goes through the same `reconcile()` gate a freshly
extracted order does, so it skips the model, not the checks.
