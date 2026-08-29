# What happens when you run `uv run fic run <image>`

A code-level walkthrough of one complete run, from the shell command to the last
saved record. For *how to operate it*, see [HOW_TO_RUN.md](HOW_TO_RUN.md); for *why
the code is shaped this way* — every bug found against the real app — see
[README.md](README.md).

```bash
uv run fic run data/input/order_001.png
```

---

## The shape of it

```
cli.py  ──►  extraction.py  ──►  session.py  ──►  flow.py  ──►  report.py
 parse        image → JSON        attach to        5 phases      runs/<ts>/
 args         + arithmetic        Fakturama        of UI work
              gate
```

Two ideas run through the whole thing:

**Nothing advances on an assumption.** Every value written to the UI is read back and
compared before the next step. A write that silently didn't take is the failure mode
this system exists to prevent.

**Stopping is a success.** When the data is ambiguous or the app disagrees, the run
raises `ManualReviewRequired` and exits. It never guesses at a financial record.

---

## Step 0 — the command starts

`uv run` resolves `.venv` from `uv.lock` and runs the `fic` entry point
(`pyproject.toml → [project.scripts] fic = "fic.cli:app"`).

`cli.py` calls `load_dotenv()` **at import**, so `.env` is live before anything else
reads it. That matters for one line in particular:

```python
DEFAULT_PROVIDER = os.environ.get("FIC_PROVIDER", "gemini")
```

The provider default is read from your `.env`, not hardcoded — so `FIC_PROVIDER` and
`FIC_GEMINI_MODEL` change behaviour with no code edit.

`run()` then validates that you passed **either** an image **or** `--from-json`, and
creates `runs/<UTC-timestamp>/`, printing the path as its first line.

---

## Step 1 — the image becomes data (`extraction.py`)

```
extract_and_reconcile(image, provider="gemini")
   ├─ extract_with_gemini(...)      the model reads the image
   ├─ parse_extraction(...)         → pydantic SourceOrder      ← tier 1
   └─ reconcile(order)              → cross-field arithmetic    ← tier 2
```

**The model call.** The image bytes plus `SYSTEM_PROMPT` plus the JSON Schema
generated from `SourceOrder` go to Gemini, which must return only JSON. The prompt's
rules are strict on purpose: transcribe verbatim, never infer, both addresses always
separately, `PAID`/`UNPAID` exactly.

Wrapped in `_gemini_generate`, which retries **transient** failures (503 overload,
429 rate limit) with backoff — 2s, 4s, 8s, 16s. Anything else (bad key, retired
model) fails immediately, with `_gemini_hint()` translating the error into one
actionable sentence instead of an SDK traceback.

**Tier 1 — structure.** `SourceOrder.model_validate` enforces what a *type* can:
non-blank SKU, quantity > 0, `discount_pct` within 0–100, and `PAID` requires a
`payment_date`. A failure here is an `ExtractionError` — the extraction was malformed.

**Tier 2 — arithmetic.** `reconcile()` checks what types can't:

```
per line   quantity × unit_net × (1 − discount/100) == line_net_total
totals     Σ lines − order discount + shipping     == net_total
VAT        Σ per-line VAT (+ shipping VAT)         == vat_total
gross      net_total + vat_total                   == gross_total
```

within a 0.02 tolerance, because three independently-rounded values are being
compared. A failure here is `ManualReviewRequired` — the *data* disagrees with itself.
**Nothing is ever auto-corrected.** A plausible-looking wrong number written into a
financial record is the worst possible outcome, so the run stops instead.

The validated order is written to `runs/<ts>/extraction.json`, and with `--dry-run`
the run ends here having touched no UI at all.

> `--from-json` skips only the model call. The JSON still passes through `reconcile()`
> — it skips the LLM, not the checks.

---

## Step 2 — connecting to Fakturama (`session.py`)

`launch_or_attach()` tries `Application.connect` first; if Fakturama isn't running it
starts it and warns that a cold start takes 1–2 minutes.

Then one polled loop answers *"which window, and is it ready?"* **as a single
question**:

```python
for win in self._candidate_windows():          # every title match, not the first
    find_by_name(win, "Create: New Order", ...) # the window that HAS the toolbar
```

Both halves matter. During startup a splash window also matches on title, so binding
to the first match and waiting for its toolbar waits forever. The window that
*contains the toolbar* is by definition the real one. A heartbeat prints every 15s so
a slow start reads as progress rather than a hang.

---

## Step 3 — the five phases (`flow.py`)

`run_flow()` threads one `RunState` through every phase. `state.note()` both appends
to the log and prints live, so you watch it happen.

### Phase 1 — open the Order

Clicks `Create: New Order`, waits for the tab, then:

- reads the proposed `No.` and **leaves it alone** (brief §1.4)
- **pre-checks that number against the database** — if Fakturama has proposed one it
  already used (its counter drifts behind its data after an unclean shutdown), the run
  stops *here* rather than after building the whole order
- writes `Date` via `set_segmented_date` — a 3-segment spinner, not a text field:
  click the left edge, 2 digits, `{RIGHT}`, 2 digits, `{RIGHT}`, 4 digits, `{TAB}`
- writes `Cust.Ref.`, sets price mode `Net` and VAT mode `With VAT`

### Phase 2 — the debtor

```
_resolve_search_key()  →  contact_resolver.resolve()   reads Database.script directly
try_select_debtor()    →  UI dialog, five-field exact match
   └─ no match → create_debtor() → re-select to PROVE the save
```

The resolver exists because Fakturama's search box matches one column at a time with
no cross-column AND — so a full company string often finds nothing. It reads the
HSQLDB file directly and resolves in two tiers: **exact company name** first
(case-insensitive, whitespace-trimmed, never partial); failing that, the five-field
AND plus a name+street check mirroring Fakturama's own duplicate rule.

It is **advisory only** — it chooses *which string to type into the search box*.
`try_select_debtor` still verifies Company + First + Last + ZIP + City against the
real dialog rows before selecting, so the resolver can't cause a wrong customer to be
billed.

Creation handles the whole Contact editor, including the second delivery address
(revealed by unchecking a box, and rendered as a *mirrored column with identical
labels* — so every delivery write pins its anchor by position). Success is proven by
**re-selecting the contact from the Order**, never by a database peek: brief §2.13.

### Phase 3 — the products, per line

```
resolve_product_line(item)
   ├─ search the "Select a product" dialog by SKU
   ├─ miss → ensure_vat(item)      reuse, else create the TAX Rate
   │         create_product(item)  gross = unit_net × (1 + vat/100)
   │         re-search
   ├─ click_row(match)  ←  selecting ≠ reading; OK on an unselected row does nothing
   └─ complete_line(item)
```

`complete_line` writes into the Items grid — a real JFace `TableViewer`. A cell editor
is activated by **two separate single clicks** (row, then cell), not a double-click,
not F2. The transient `Edit` widget isn't parented under the row, so it's found by
diffing the window's `Edit` elements before and after.

Then quantity, a confirm-or-write of U.Price, a VAT cross-check, discount — and
finally the line `Price` is compared against `expected_line_net()` computed
independently. That last comparison is what proves the write reached Fakturama's data
model rather than just its display.

### Phase 4 — verify the totals, then save

Reads the totals block and compares all three:

| Field | Compared against |
|---|---|
| `Total Net` | items net (**excludes shipping** — Fakturama's convention) |
| `VAT` | `vat_total` |
| `Total` | `gross_total` (**includes** shipping) |

Order-level `Discount` is checked and `Shipping` is written when the document charges
it. Only if everything matches does `actions.save()` run — which activates the target
tab first (the toolbar Save acts on whichever editor is *active*), then confirms the
tab's dirty `*` marker cleared, watching for a blocking dialog throughout.

### Phase 5 — the linked Invoice

`ensure_payment_method` runs first, idempotently, so the method exists before the
Invoice needs it. Then the Invoice is created from the **Order's own "Create a
duplicate" panel** — that is what preserves the Order↔Invoice link in the database
(both rows share a transaction id). The toolbar's own Invoice button would produce an
unlinked document.

The inherited totals are re-verified, the payment method selected (never created here
— brief §5.2), and if the document says PAID the `paid` box is ticked, which *swaps
the panel* to reveal the date field and `Value`. Then save.

---

## Step 4 — what you're left with

```
runs/<UTC-timestamp>/
├── extraction.json   what the model read from your image
├── state.json        every step, the order number, what was created vs reused
├── report.md         human-readable summary
└── trace.jsonl       error events, if any
```

Exit codes: `0` success · `2` manual review · `3` verification failed · `4` control
not found · `5` extraction failed.

**`state.json` is the first thing to read after a failure** — it shows exactly how far
the run got and what it did.

---

## Where the safety actually lives

| Guard | Catches |
|---|---|
| pydantic (tier 1) | malformed extraction — blank SKU, PAID with no date |
| `reconcile()` (tier 2) | a document whose own arithmetic disagrees |
| read-back after every write | a field that silently rejected or reformatted the value |
| `expected_line_net()` per line | a grid write that reached the display but not the model |
| totals compared before Save | any line corrupted earlier in the run |
| re-select the debtor after creating | proof the save really landed |
| ambiguity → `ManualReviewRequired` | two candidate customers, a duplicate document number |

The recurring lesson from building this, recorded in the README: **a check pointed at
the wrong object can only ever answer "not yet".** Two of the worst bugs — a combo
that verified its own display text while the model kept the old value, and a toolbar
lookup aimed at a splash window — both hid behind verification that was technically
running and structurally meaningless.
