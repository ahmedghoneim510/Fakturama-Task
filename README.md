# Fakturama Image-to-Cash Automation

One continuous Order-first flow: a single order image → extracted structured data →
resolved/created Debtor → resolved/created Product+VAT per line → saved, verified
Order → linked Invoice with payment status applied. Built for the TJM take-home
assignment; design rationale lives in [`docs/design.md`](docs/design.md) (the Part 1
deliverable) and [`docs/reference/`](docs/reference) (the full working engineering
spec, corner-case catalog, and build plan this implementation was built from).

**Read this section before anything else:** this README's "Live verification status"
table (below) is the single most important part of this document. A meaningful chunk
of this codebase was driven against a real, running Fakturama installation during
development — not written blind — and that process found and fixed several real bugs
that would not have been caught by code review alone. The table says exactly what's
confirmed working, what's fixed-but-not-independently-reconfirmed, and what's
implemented but never exercised against the real app.

---

## Setup

### Dependencies

- Windows 10/11 (Microsoft UI Automation is a Win32 API — this does not run on
  Linux/macOS for anything touching `fic.uia`; `fic extract` alone is platform-neutral).
- [Fakturama](https://www.fakturama.info/download/) installed. Auto-detected from the
  usual `Program Files` locations; override with `FIC_FAKTURAMA_EXE` in `.env` if yours
  differs.
- Python 3.10+.

### `uv` (the intended workflow)

```bash
uv sync                 # builds .venv from pyproject.toml + uv.lock, installs `fic`
cp .env.example .env    # then fill in GEMINI_API_KEY
uv run fic --help
```

Everything runs through `uv run` — `uv run fic ...`, `uv run pytest`, `uv run ruff`.
See [HOW_TO_RUN.md](HOW_TO_RUN.md) for the operator-facing guide.

**A note worth keeping, because it wasted real time.** For most of this project's
development `uv sync` appeared to hang forever, and the work was done under a plain
`venv` + `pip` fallback with that recorded here as an unexplained environment quirk.
It was not a quirk and it was not dependency resolution: `uv.exe` on this machine was
a **truncated 538 KB binary** (a healthy one is ~41 MB), so *any* invocation hung —
even `uv --version`, which never printed a byte. The give-away was finding a `uv`
process still stuck **nine hours** after the first attempt, holding the file open so
the installer couldn't even replace it. Reinstalling fixed it outright; `uv sync` now
completes in seconds and the whole app runs under `uv run`.

The lesson is about diagnosis, not uv: "`uv sync` hangs" was accepted as an
environment fact and routed around for an entire session, when one `uv --version`
would have shown the binary was broken. A tool that hangs on its *simplest* command
isn't slow, it's broken — check that before blaming the workload.

<details>
<summary>Without uv (fallback)</summary>

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip   # 21.x can't do PEP 660 here
.venv\Scripts\python.exe -m pip install -e .
copy .env.example .env
.venv\Scripts\fic.exe --help
```
</details>

### `.env`

```
GEMINI_API_KEY=...                 # required (default provider)
ANTHROPIC_API_KEY=sk-ant-...       # required only for --provider claude / --cross-check
FIC_PROVIDER=gemini                # gemini | claude
FIC_GEMINI_MODEL=gemini-flash-latest
FIC_FAKTURAMA_EXE=                 # optional, only if auto-detection fails
```

---

## Running it

```bash
fic extract data/input/order_001.png -o out.json    # extraction only, no UI (needs an API key)
fic probe                                           # dump the live UIA tree of the focused Fakturama window
fic run data/input/order_001.png                    # full flow, image -> Order + Invoice
fic run data/input/order_001.png --dry-run          # extract + reconcile, stop before touching the UI
fic run data/input/order_001.png --cross-check      # Claude + Gemini, diff money fields (needs GEMINI_API_KEY)
fic run data/input/order_001.png --provider ocr     # local OCR, no API key (needs `uv sync --extra ocr`)
fic run data/input/order_001.png --cross-check --check-with ocr   # LLM + OCR must agree on money

# Run from an already-extracted order JSON instead of an image -- no API key needed
fic run --from-json data/golden/order_002.json
fic run --from-json data/golden/order_002.json --dry-run
```

If the `fic` entry point isn't on your PATH, the module form works identically:
`python -m fic run --from-json data/golden/order_002.json` (with `PYTHONPATH=src`, or
after `pip install -e .`).

**`--from-json` replaces only the extraction step.** Everything downstream is
unchanged — the JSON goes through the same tier-2 `reconcile()` gate a freshly
extracted order does, so it skips the LLM, not the checks. It exists because the
extraction and the automation are independently useful: it makes runs reproducible
(the same payload every time, no model variance) and lets the whole UI flow be
developed and tested without an API key.

`fic probe` was, by a wide margin, the single most valuable command built for this
project — nearly every fix in the table below started with running it against the
real app to see what was actually there, rather than guessing from the brief's
screenshots (which turned out to be from a different Fakturama build/locale than the
one available to test against — see below).

---

## Tests

```bash
pytest                # unit tests only (default) -- pure logic, no GUI, no network, ~2s
pytest -m live         # + a real Claude API call (needs ANTHROPIC_API_KEY)
```

There is no `-m e2e` suite. Given the amount of real, version-specific UI behavior
this build's Fakturama install turned out to have (see below), a scripted e2e suite
written against assumptions would have been testing the wrong thing; the live
verification that *did* happen was done by directly driving `flow.py`'s functions
against the real app during development, which is what the table below reports on.
Writing a proper `pytest -m e2e` suite against a resettable workspace is the top item
in "if I had 3 more hours" below.

40 unit tests pass, covering: pydantic tier-1 validation (PAID-without-date, zero/
negative quantity, unrecognized payment status, blank SKU), tier-2 reconciliation
(line/order-total tolerance checks), the gross-price canary from the golden fixture
(€250.00 × 1.19 = €297.50 — confirmed to match the brief's own screenshot exactly),
the extraction pipeline's schema validation with a mocked Claude response, and the
DB-backed contact resolver (18 tests: AND-matching, Arabic normalization, the
duplicate-NR scenario proven live — see below).

---

## Live verification status

This is the honest accounting the brief asks for. Fakturama was actually installed
and running on the development machine, so this wasn't written blind — but the two
Live-verified rows below required extensive iterative debugging against real,
version-specific behavior that did not match the brief's own screenshots (this
installed build is Fakturama 1.6.9, US-localized — `$` not `€`, `MM/DD/YYYY` dates —
and its Contact/Debtor editor layout differs structurally from what the brief's
figures show; see "Real bugs found live" below).

| Brief step | Status | Notes |
|---|---|---|
| **1. Extract + open New Order** | ✅ Live-verified | No./Date/Cust.Ref/price-mode/VAT-mode all confirmed writing and reading back correctly against the real app |
| **2. Debtor — select path** | ✅ Live-verified | Both branches proven: correctly returns "no match" for a new company, and correctly finds an exact match (including on a debtor created by an *earlier* run — proves the whole write→save→search→match round trip). A real bug was found and fixed here late: the matched row was never actually clicked/selected before OK, only read — see "Real bugs found" below |
| **2. Debtor — creation branch** | ✅ Live-verified, both address cases, incl. the full round trip | Full sequence (Company, paired First/Last Name fields, Street, paired ZIP/City, Country, checkbox, Miscellaneous tab, Save, tab-reactivation, reselection) completed successfully in one continuous run. The **differing-delivery-address** branch is now implemented and live-verified too (#38): unchecking "Delivery Address equals Invoice Address" reveals a mirrored second column with duplicate labels, so every delivery write pins its anchor by position. Until it was verified this branch deliberately raised `ManualReviewRequired` rather than guess — refusing was the right behaviour while the UI was unconfirmed, and it is only now replaced because the layout was actually checked |
| **2. Payment-method sub-branch** | ✅ Live-verified | `ensure_payment_method` was reached for real during a Contact creation: it navigated to `Payments` (the nav name had to be fixed first — see #27), created and saved a "Bank Transfer" payment via the menu route, and a later run selected that method from the combo with no error logged |
| **3. Product/VAT select-or-create** | ✅ Live-verified, both branches | **Select path:** SAMPL01 resolved from the product master and added correctly. **Create path:** a genuinely new SKU drove VAT-reuse → `New > New Product` → save → re-search → select, producing a correct line. Proven in one order containing *both* an existing and a new product — see the two-line grid result below. Getting here required fixing five separate bugs (#22, #23, #24, #26, #27) |
| **3. Line completion (Qty/Price/VAT/Discount)** | ✅ Live-verified, fully working | The single biggest open question in the whole design is now answered completely, not just half: writing to the grid works. The activation is two **separate** single clicks (row, then cell — not a double-click, not F2), found by checking Fakturama's own source (github.com/hernad/fakturama) for its `DocumentItemEditingSupport` class, which confirmed a standard JFace `TextCellEditor` and pointed at the right click pattern instead of more blind guessing. Full round trip proven live: `Qty=4, Discount=10%` on a €1.90 item produced `Price=6.84 $`, exactly matching the independently-computed `expected_line_net()` |
| **4. Save + verify Order totals** | ✅ Live-verified | A real 2-line order verified **all three** totals exactly — `Total Net 17.30` / `VAT 3.29` / `Total 20.59` against the independently-computed `17.30 / 3.29 / 20.59` — plus order-level Discount, then saved. Confirmed three ways: the tab renamed `*New Order` → `PO000003`, `state.order_saved` went True, and the row is in the database (`DOCUMENTS(4, …, 'TJM-PHASE4-1', '2026-08-28')` with the right contact id linked). The old VAT "discrepancy" (#21) turned out to be the price-mode combo silently failing, not a VAT-computation question at all |
| **5. Follow-up Invoice + payment state** | ✅ Live-verified | Invoice created from the saved Order's "Create a duplicate" panel (#32), inheriting both item lines and all three totals — re-verified against the source, not trusted. Payment method set to `Bank Transfer` (the combo defaults to `Pay Cash`, #33), `paid` ticked, payment date and `Value 20.59 $` applied (#34), then saved: tab renamed `*New Invoice` → `INV000001`. **The Order↔Invoice link is confirmed in the database**, not just assumed from using the right button: `PO000005` and `INV000001` share transaction id `7` and item rows `'8,9'`, while differing correctly on doctype (3 vs 5), paid (FALSE vs TRUE), date and method |

### Phases 1–5, verified as one continuous cycle

The four branches that matter were driven end to end against the real app, in two
runs covering both sides of each decision:

**Run A — existing customer, one existing product + one brand-new product in the
same order.** Final Items grid, read back from the live UI:

| SKU | Qty | VAT | U.Price | Discount | Price | expected |
|---|---|---|---|---|---|---|
| `SAMPL01` (selected) | 2 | 19 % | 1.90 $ | 0 % | **3.80 $** | 2 × 1.90 ✓ |
| `TJMNEW02` (created) | 3 | 19 % | 5.00 $ | −10 % | **13.50 $** | 3 × 5.00 × 0.9 ✓ |

Both lines present — the earlier "only one product applies" symptom was the run
aborting before the second item was ever reached, not a grid limitation. The created
product's master gross price came out `5.95` (= 5.00 × 1.19 ✓) with VAT `19 %`.

**Run B — brand-new customer.** `try_select_debtor` correctly returned "no match",
`create_debtor` wrote and saved the contact, and it was then **re-selected from the
Order by exact match on all five fields** (Company, First Name, Last Name, ZIP, City)
— the write→save→search→match round trip, which is the brief's own definition of a
confirmed save (§2.13). The product line then applied at `Qty=4 → Price 7.60 $`.

**Run C — all four phases in one pass, ending in a real Save.** Same two-line order,
carried through to completion:

```
PHASE 1  order opened, proposed No. = 'PO000003'
PHASE 2  debtor selected: exact match on all five fields (contact id 5)
PHASE 3  SAMPL01  qty=2 discount=0%  price=3.80 $
         TJMNEW02 qty=3 discount=10% price=13.50 $
PHASE 4  totals verified (net 17.30, VAT 3.29, gross 20.59) -> SAVED
         tab renamed '*New Order' -> 'PO000003'
```

Verified independently of the flow's own reporting, in the database:
`INSERT INTO DOCUMENTS VALUES(4, 'Zenith Testing Corp Step4\nZoe Zimmerman\n…',
…, 5, 3, '', 'TJM-PHASE4-1', '2026-08-28', …)` — correct linked contact, Cust.Ref and
date. (HSQLDB writes to `Database.log` first and folds it into `Database.script` at
checkpoint, so a freshly-saved row is found in the former.)

**Run D — all five phases in a single process, through the real `run_flow()`.** This is
the shipping entry point the CLI calls, not a test-only arrangement of the step
functions:

```
order_no        = PO000007
debtor_resolved = True
products        = ['SAMPL01', 'TJMNEW02']
order_saved     = True
invoice_saved   = True
tabs            = ['Start', 'PO000007', 'INV000003', 'Documents', 'Welcome']
```

with the run log recording, in order: header set → debtor matched via the resolver →
both lines completed (`3.80 $`, `13.50 $`) → `order PO000007 saved; totals verified
(net 17.30, VAT 3.29, gross 20.59)` → `linked Invoice opened from the Order's 'Create a
duplicate' panel` → `payment method set to 'Bank Transfer'` → `marked paid on 2026-08-28
with value 20.59` → `invoice saved`.

Confirmed in the database afterwards — including the **link**, which is the part that
would be easiest to fake by using the wrong button:

```
PO000007   transaction=11  items='12,13'
INV000003  transaction=11  items='12,13'
INV000003  paid=TRUE payDate=2026-08-28 method='Bank Transfer' value=20.59
```

**Run E — a second, independent order through the real CLI.** `order_002.json` differs
from the sample in every way that matters: a **separate delivery address**, a **20.00
shipping charge**, an **UNPAID** payment status, and **two products that did not exist**.
Run as `fic run --from-json data/golden/order_002.json`, it drove all five phases to
`done — Order + Invoice saved and verified`:

```
order_no        = PO000009
products        = ['MON-4K-27', 'KEY-WRL-08']     (both created)
order_saved     = True
invoice_saved   = True
  line for MON-4K-27 completed: qty=2 discount=0% price=700.00 $
  line for KEY-WRL-08 completed: qty=3 discount=0% price=240.00 $
  order shipping set to 20.00
  order PO000009 saved; totals verified (net 940.00, VAT 182.40, gross 1142.40)
  invoice left unpaid -- no date/value invented, per brief S5.3
```

Link confirmed in the database: `PO000009` and `INV000004` share `transaction=14` and
items `'16,17'`, with the invoice carrying `method='Bank Transfer'` and correctly left
unpaid. This run is what surfaced bugs #36–#41 — a reminder that a second document
exercises paths the first never touches.

**Legend:** ✅ live-verified = actually run against the real Fakturama install and
confirmed working. 🟡 half-resolved = part of it is live-confirmed working, part
is a live-confirmed, specific dead end (not an unexplored guess). 🔶 implemented,
not live-verified = written using the same patterns/primitives as the verified
code, calibrated against real field names where those were checked via
`fic probe`, but never actually executed end-to-end. ❌ not implemented = an
honest gap, not a hidden one.

---

## Contact resolution: `uia/contact_resolver.py`

Added after a live session surfaced two real problems with resolving a Debtor by
name:

1. **Fakturama's search box does single-string substring matching per column, with
   no cross-column AND and no tokenization.** A multi-word name query ("Ahmed Ali")
   only matches if that whole substring sits in one column — it fails whenever first
   and last name are (as normal) in separate `FIRSTNAME`/`NAME` columns.
2. **The displayed "Customer ID" (`NR`) is not a reliable unique key.** Proven live,
   directly in this project's own test data (see `docs/reference/` for the raw
   evidence): force-restarting Fakturama between runs caused it to reissue an
   already-used `NR` for a genuinely different contact. This is almost certainly why
   Fakturama has its own native Name+Street duplicate check — it can't fully trust
   its own numbering either.

The resolver reads `Database.script` (Fakturama's HSQLDB persistence file) directly,
parses the real `CONTACTS` table (column order derived from the live `CREATE TABLE`
line, not hardcoded), and does proper field-by-field AND matching — no substring
search involved at all, so the tokenization problem doesn't arise. It also includes
Arabic name normalization (alef/hamza variants, tāʾ marbūṭa, tatweel, diacritics)
for exactly this kind of matching against Arabic customer names.

**This does not replace the UI as the existence check** (brief §2.13: successful
selection from the Order confirms the save, never a DB peek). It's wired in as
purely advisory: `try_select_debtor()` now asks the resolver first, and if it finds
exactly one match, searches Fakturama's own dialog by that contact's **last name**
(confirmed to match correctly) instead of company (confirmed to fail on multi-word
queries) — the actual selection and its read-back verification, unchanged, is still
what actually happens. If the resolver finds more than one match, the flow stops for
manual review *before* even opening a UI dialog, which also sidesteps ever
triggering Fakturama's own blocking duplicate-contact popup. If the resolver can't
run at all (file not found, wrong machine, parse failure), it falls back to the
original company-based search — a resolver failure degrades search precision, it
never blocks the flow.

**Verification status:** the resolver itself — parsing, AND-matching, Arabic
normalization, duplicate-NR detection — is live-verified: it was run directly
against this machine's real `Database.script` and correctly parsed all 5 real
contact rows, correctly flagged the real `CUST000001` NR collision, and correctly
recommended "Klein" as the search key (18 unit tests also cover this in isolation).
The *wiring* into `try_select_debtor()` (actually driving the UI search with the
resolver's recommended key) is implemented but was not independently re-run live
after being wired in, given the time remaining in the session.

---

## Real bugs found and fixed during live testing

Listed because *how* these were found matters as much as the fixes — none of these
would have been caught by reading the code, and several are exactly the kind of thing
that silently corrupts a saved record rather than crashing loudly:

1. **Icon elements were `Image`, not `Button`.** The brief's "upper icon beside
   Addresses" / "upper icon beside Items" controls resolve as UIA `Image` elements.
   The locator's icon-cascade originally only looked for `Button`, so it would have
   found zero candidates every time.
2. **Every Fakturama dialog is a nested `Window`-type element inside the main
   window's own UIA tree, not a separate top-level OS window.** This is the single
   biggest structural finding. The original design (informed by the brief's
   screenshots) assumed `Desktop().windows()` would find dialogs like "Select the
   address" — it never does, because this is a single-window Eclipse RCP app.
   `wait_nested_window()` replaces that entire approach.
3. **`window_text()` silently returns the wrong thing on several controls.** UIA's
   `ValuePattern` (`.get_value()`) is the correct read for live field content;
   `window_text()` reads the *static accessible Name*, which for some controls (e.g.
   the Cust.Ref field) never changes regardless of what's typed — confirmed live: it
   returned `'Cust.Ref.'` forever, with no error, making every prior read-back
   assertion against it meaningless. `actions.read_value()` was rewritten to try
   `get_value()` first.
4. **The Date field is a genuine 3-segment spinner control (month/day/year), not a
   free-text field.** Typing a full date string with separators silently corrupts it
   into an unrelated date. The correct recipe (found empirically): click the left edge
   to focus the month segment, type 2 digits, `{RIGHT}`, 2 digits, `{RIGHT}`, 4 digits,
   `{TAB}`.
5. **The date-writing bug above was originally landing on the wrong control
   entirely** — a "Gross"/"Net" price-mode dropdown sits immediately right of the Date
   field with no distinguishing content of its own, and an early version of the label
   search reached past the Date field onto it, silently overwriting it with typed date
   text. **A user watching the actual screen live caught this** — it would not have
   been caught by any of this project's own automated checks, which is itself a useful
   data point about the limits of read-back verification when the write lands on the
   wrong field entirely rather than being rejected.
6. **`select_combo()` assumed a real openable dropdown.** Some of Fakturama's combos
   (the price-mode field, confirmed) are text-typable pseudo-combos where `.expand()` +
   `.texts()` returns only an inner "Open" button's caption, not real options. Fixed to
   try the real-dropdown path first and fall back to type-and-verify.
7. **`save()`'s dirty-marker check was silently checking the wrong window's title.**
   Every caller passes `session.main_window` (the whole-app scope every editor
   resolves to) as the "editor" — and the main window's own title never carries
   Fakturama's `*` unsaved-marker; only individual `TabItem` titles do. This meant the
   save-verification the whole design is built around was, for a period during
   development, actually a no-op that always reported success immediately. Fixed to
   scan the real `TabItem` elements.
8. **Fakturama has its own native duplicate-contact detector** (Name+Street based —
   deliberately a *different, narrower* rule than this project's own brief-mandated
   Company+First+Last+ZIP+City exact-match), and it does not use the nested-`Window`
   dialog pattern every other Fakturama dialog in this build uses — it's a genuine
   separate top-level OS window. It also fires **live, on tab-out of the Street field**,
   not only at final Save, and its popup steals keyboard focus — confirmed live to
   silently truncate the *next* field being typed into ("Berlin" landed as "B"). Two
   things came from this: `save()` now diffs the set of top-level windows before/after
   the click (title-based detection doesn't work — this dialog is generically titled
   just "Fakturama"), and the same guard was added right after the Street write, the
   confirmed trigger point. Both route to `ManualReviewRequired` rather than clicking
   through, on the reasoning that only a human can tell whether a Name+Street
   collision means "this is the same customer, reuse the record" or "coincidence,
   proceed" — automating that guess risks silently merging two different customers'
   data or creating a real duplicate.
9. **The Contact/Debtor editor's actual field layout doesn't match the brief's
   screenshots at all.** "First Name" and "Last Name" are one combined label over two
   side-by-side Edit controls; same for "ZIP" and "City". "Country" is plain free text,
   not a dropdown. There's a single "Delivery Address equals Invoice Address" checkbox
   rather than separate per-address Invoice/Delivery role checkboxes. "Salutation" is
   called "Gender". This all had to be discovered live, field by field.
10. **The database workspace isn't where this project's own earlier config
    comments assumed.** `config/app.yaml`'s notes referenced
    `%USERPROFILE%\Fakturama2\` (from the brief/general Fakturama documentation);
    the real live path on this machine is `%USERPROFILE%\Database\Database.script`.
    Found only by actually looking, while building the contact resolver above.
11. **Multiple simultaneously-open tabs break tab-scoped field resolution**, and this
    is not hypothetical — it happens on every debtor creation, since the New Order tab
    is deliberately kept open per the brief while the New Contact tab is also open.
    After saving the Contact, Fakturama's *active* tab is the Contact, not the Order,
    and the Order's own fields/icons are unresolvable until its tab is explicitly
    reactivated. Fixed by reactivating the Order tab before any post-creation
    reselection.
12. **Reading a dialog row's text is not the same as selecting it.** The single most
    consequential bug found this session: both `try_select_debtor()` and
    `resolve_product_line()` searched a dialog, read the matching row's values via
    UIA, confirmed it was the right one — then clicked OK directly, without ever
    clicking the row itself to select it. Confirmed live: this doesn't error, doesn't
    warn, just silently does nothing — the dialog closes and nothing gets added. Found
    while investigating why an Order's Items grid stayed at "0.00 $" after apparently
    successfully selecting a product. `locator.read_table_rows()` now also returns
    each row's own clickable wrapper (`"_row"`), and `locator.click_row()` selects it
    before OK is invoked, at both call sites.
13. **The "Select a product" dialog has 6 columns, not 3.** The assumed
    `["Item No.", "Name", "Price"]` was missing `Category` and `Description`, which
    silently shifted every value read from the dialog one-or-more columns to the
    right (a search result's Category showed up where Name was expected, and Name
    showed up where Price was expected). The SKU exact-match check happened to still
    work despite this (SKU is always column 0), which is exactly why it went unnoticed
    until the row-click bug above forced a closer look at the whole dialog.
14. **The Items grid is a real, accessible `List`/`ListItem`/`Text` tree — not
    canvas-drawn**, resolving the single biggest open question in the whole design
    (see `docs/reference/`). Confirmed by actually getting a line added (once bug #12
    was fixed) and reading its values straight out of the UIA tree.
15. **The grid cell-write mechanism, fully solved.** Nine interaction patterns were
    tried live and ruled out (click, double-click cell, F2-on-cell, click+type,
    double-click row, F2-on-table, F2-on-row, two-separate-clicks-without-checking-
    whole-window, select+Enter) before the actual answer was found by checking
    Fakturama's own source on GitHub (`hernad/fakturama`) instead of continuing to
    guess: it's a plain JFace `TableViewer` with `TextCellEditor`s, activated by
    **two separate single clicks** — click the row to select it, then a second,
    distinct click on the target cell (JFace's `MOUSE_CLICK_SELECTION` activation
    event). The transient editor is a genuine `Edit` element but is NOT parented
    under the row/grid in the UIA tree — it has to be found by diffing the set of
    `Edit`-type elements across the whole editor window before/after the click, not
    by searching within the row. Reading source code once beat nine more rounds of
    blind trial and error; worth remembering for the next similarly-stuck problem.
16. **`editor.descendants(control_type="List")[0]` isn't safely "the Items grid."**
    More than one `List`-type element can exist in the tree; taking the first one
    blind silently picked an unrelated (empty) list in at least one live run, which
    made an accessible grid falsely report as "canvas-drawn." Fixed by identifying
    the real Items grid by its own header content (`Qty.` + `Item No.` present)
    instead of position.
17. **Fakturama displays Discount as a *negative* percentage** ("-10 %" for a typed
    "10") — a real display convention (a discount is a negative price adjustment),
    not a sign error in the write. The read-back comparison now strips sign before
    comparing, since no field this project writes is ever meaningfully negative.
18. **Grid cell-edit activation is timing-sensitive right after a dialog closes.**
    The exact click sequence that reliably worked in isolation failed when run
    immediately after the product-selection dialog closed with no settle time.
    Fixed with a real condition to poll for (the new line actually present in the
    grid, matched by SKU) before attempting to edit it, rather than a fixed delay.
19. **`"Total Gross"`/`"Total"` display the NET figure under "Net" price mode, not
    gross.** Confirmed against a real order (4×SAMPL01 @ €1.90, 10% discount): both
    fields read exactly `6.84` (the net total), not `8.14` (gross). An earlier
    version of `complete_and_save_order()` compared them against
    `source.gross_total` and would have failed every single real order regardless
    of correctness. Fixed to compare against `source.net_total`, and re-confirmed
    live afterward with zero mismatches.
20. **`Save()`'s 15s timeout was too tight for a large-payload save.** A full Debtor
    creation (every field set, Miscellaneous tab, Payment method) genuinely
    succeeded — the tab's dirty marker cleared, no dialog appeared — but took longer
    than 15s under this run's load, so the timeout fired on a save that had actually
    worked. Raised to 30s. A pointer for anyone hitting this again: check the tab
    title for the dirty marker directly before assuming a real hang.
21. **The "VAT reconciliation discrepancy" — SOLVED, and it was never a VAT bug.**
    On a real order (net 6.84, 19% VAT) Fakturama displayed VAT `1.09` where this
    project's formula gives `1.30`, and I logged it as an unexplained open question.
    It has an exact arithmetic explanation: `6.84 / 1.19 × 0.19 = 1.09`. Fakturama was
    computing the VAT *contained within* 6.84 — i.e. treating that figure as **gross**,
    because the order was still in **Gross price mode**. `select_combo`'s old
    type-and-verify path had silently failed to commit "Net" (§1.7), exactly the same
    failure later caught on the product VAT combo (#24) — it "verified" against the
    display text and reverted. Two further things fall out of this, both of which had
    been papered over: the totals label is **mode-dependent** (`Total Gross` in Gross
    mode, `Total Net` in Net mode), and an earlier "fix" that compared *both*
    `Total Gross` and `Total` against `net_total` was really compensating for the wrong
    price mode. With the combo fixed, a live run reads `Total Net 17.30 / VAT 3.29 /
    Total 20.59` against expected `17.30 / 3.29 / 20.59` — an exact three-way match,
    no tolerance consumed. **Lesson worth keeping:** a read-back that reads the same
    widget you just wrote can confirm your own mistake; this one stayed hidden for a
    whole session behind a "documented open question" that was really a silent bug two
    steps upstream.
30. **Two controls can share a label, and the first one won.** `resolve_by_label` took
    the first matching anchor, but the Order editor has a `VAT` Text beside the header
    mode combo *and* another `VAT` Text in the totals block — so the totals check
    silently measured the wrong control. Added an explicit `pick="last_by_top"` used
    by the totals lookups (the totals block is the bottom-most). Note this is a
    different failure from `AmbiguousControl`: that guards two *candidates* near one
    anchor; this was two *anchors* with the same text.
31. **Fakturama can propose a document number that already exists.** Save was rejected
    with its own dialog, *"There is already a document with the number: PO000001"*.
    Cause: `NUMBERRANGE_ORDER_NR` in the `PROPERTIES` table is persisted on **clean
    shutdown**, but this session force-killed Fakturama repeatedly — so the counter
    stayed at `1` while `DOCUMENTS` rows had advanced to `PO000002`. The flow's
    behaviour here was already correct and is worth stating: it refused to save,
    surfaced Fakturama's exact dialog text, and did **not** retry or invent a new
    number (the brief forbids altering the proposed No., §1.4). Worth knowing for
    anyone re-running this: shut Fakturama down cleanly, or the counter desyncs.

### Round 2: exercising the *creation* branches end to end

Everything above was found while getting the **select** paths working. Driving the
**creation** paths (a product that genuinely doesn't exist yet) surfaced a second
cluster — including one bug that had been misdiagnosed above, and one that defeated
the read-back rule this whole project leans on.

22. **The green `+` button on every list view is unreachable, and the creation
    branches were wired to it.** `find_by_name(view, "+")` never could have matched:
    live-confirmed, those buttons expose **no accessible name, no automation id, no
    MSAA `Description`, and no help text** — anonymous `Role 43` push buttons. Worse,
    clicking one (found positionally instead) produced only a transient tooltip
    window and never opened an editor, so even correct resolution was a dead end.
    Replaced with the **menu bar**: `New > New VAT` / `New Payment` / `New Product` /
    `New Contact` are all real, uniquely-named `MenuItem`s (`actions.click_menu_item`).
    One trap handled there: Fakturama's menu items are present in the UIA tree even
    while their menu is *closed*, reporting a degenerate `(0,0)` rect — clicking one
    in that state clicks the screen origin, i.e. a different application entirely. The
    helper opens the parent menu and waits for a non-zero rect before clicking.
23. **`type_keys()` is SendKeys syntax, not a literal string — and the VAT name is
    `"VAT 19%"`.** pywinauto parses `^ + % ~ ( ) { }` as Ctrl/Shift/Alt/Enter/grouping.
    The `%` in every VAT name was typed as an **Alt modifier**, so product creation
    failed on its very first field. This is not a corner case confined to VAT names:
    the project's own golden fixture carries the phone `+49 30 5550 1420`, whose
    leading `+` would be swallowed as Shift. Fixed with `actions.escape_keys()`,
    applied to every document-derived write (`set_text`, grid cell writes) but
    deliberately *not* to intentional key sequences like `{END}`.
24. **`select_combo()` silently faked success, and the read-back verified the lie.**
    The most serious bug of the session. Fakturama's combos are SWT `CCombo`s;
    `.texts()` returns `['VAT', 'VAT', 'Close']` — the label twice and the drop-down
    button's caption, never the options — so the old code always fell through to
    type-and-verify. Typing `"VAT 19%"` set the combo's *display text*, `read_value()`
    read that text back, the write "verified" — and then the value reverted on
    focus-out and **the product saved with VAT `0 %` (Tax-free)**. A silently wrong
    tax rate on a saved master record is exactly what this project's read-back rule
    exists to prevent, and here the read-back itself was the thing being fooled;
    it was caught only by inspecting the resulting order line, not the write.
    Fixed by clicking the combo's inner `Open` button and clicking the real
    `ListItem` (the options only exist in the tree while the list is open, and the
    popup is *not* parented under the combo, so it is matched positionally). A value
    genuinely absent from the open list now raises `OptionUnavailable` instead of
    falling through to typing.
25. **`save()` asked "is ANY tab dirty?" — and the brief guarantees one always is.**
    The dirty-marker check scanned every `TabItem` for a leading `*`. But the New
    Order tab is required to stay open across every detour and is dirty from the
    moment its header is set, so while saving a Contact / Product / TAX Rate the
    Order's own `*` was always present and the check could never go False. **This is
    the true cause of what bug #20 above attributed to a tight timeout** — raising
    15s → 30s changed nothing except how long it took to fail. `save()` now takes an
    explicit `tab_title` and scopes the check to that one tab.
26. **`find_grid()` hung indefinitely against a whole-editor scope.** It tried up to
    three `descendants(control_type=…)` walks (`DataGrid`, `Table`, `List`). Called
    with `session.main_window` — which the list views must use, since `editor_tab()`
    can't isolate a tab's content pane — this stalled with no timeout able to fire,
    because the block happens *inside* a single COM call rather than between polls.
    Rewritten as one unfiltered `snapshot()` pass (~179 elements, ~2s). It also now
    takes `required_headers`, because with the Order tab open its Items grid is in
    scope too and "first grid found" was a coin flip — the same positional-luck bug
    that already bit `complete_line()` once (#16).
27. **Wrong names/labels throughout the creation paths, only discoverable by opening
    the editors.** The VAT list columns are `Standard, Name, Description, Value` — the
    code had `["Name", "Value", "Standard"]`, which shifted every value over so the
    existence check compared a blank marker against the VAT name and could never
    match *any* row (so every run fell into the creation branch, which then hit #22 —
    two bugs each hiding the other). The VAT editor is called **"New TAX Rate"**, not
    "VAT", and has **no "VAT code (E-Invoice)" field at all**, so the brief's third
    reuse condition is inapplicable here. On the Product editor the real labels are
    **"Price (gross)"** (not "Price"; and its Edit has no accessible name, so the
    label is the only route to it) and **"Quantity"** (not "Stock"), and there is **no
    "cost price" field**. The payments navigator entry is **"Payments"**, not the
    brief's "terms of payment" — that name matched nothing, so `ensure_payment_method`
    could never reach even its search step.
28. **Cold start blocks UIA calls with no timeout.** `launch_or_attach()` waited only
    for the window *title*, which appears long before the workspace renders. Worse,
    polling into a still-loading Fakturama blocks inside the COM call, so `wait_until`'s
    deadline — only checked *between* polls — never fires. Added an explicit
    toolbar-ready gate with its own much larger `startup_timeout` (a warm attach
    satisfies it instantly). The residual limitation is real and documented: attaching
    to an already-running Fakturama is the reliable path.
29. **Save saved the wrong editor.** The toolbar Save button acts on whichever editor
    tab is **active**, not on the `editor_window` the call nominally targets (always
    `session.main_window` here). Confirmed live: creating a Contact triggers the
    payment-method sub-branch, which opens *and saves* a "Bank Transfer" payment
    editor and leaves that tab active — so the following Save re-saved Bank Transfer
    while the Contact stayed dirty until the timeout, reported as a save failure with
    no hint that the click had gone somewhere else entirely. `save()` now activates
    its `tab_title` tab before clicking. This is the same class as #11, but at a step
    that looked safe because no field resolution was involved.

### Round 3: Step 5 (the linked Invoice)

32. **The follow-up control isn't where the code looked, and the label it looked for
    doesn't exist.** `followup_invoice()` navigated to the **Documents view** and
    searched for a label *"Create a follow-up document"* — no such text exists anywhere
    in this build. The real mechanism is a `Group` named **"Create a duplicate"** inside
    the saved **Order editor**, holding four buttons: Confirmation / Invoice / Delivery
    Note / Proforma. Resolution is now scoped to that Group rather than matching a bare
    "Invoice" anywhere in the window, which also documents which of the four the brief
    wants. Using it is what preserves the Order↔Invoice link (see below).
33. **The Invoice's payment-method combo has no label and no name**, so
    `resolve_by_label(…, "Payment")` could never find it — there is no "Payment" text on
    that editor at all. It is the unnamed `ComboBox` on the same row as the `paid`
    checkbox, resolved positionally from it. It also **defaults to `Pay Cash`**, so it
    genuinely must be set — it is never accidentally correct, and a silent failure here
    would save an invoice under the wrong payment terms.
34. **Ticking `paid` replaces the payment panel's contents.** With `paid` unchecked the
    panel shows `Due Days` + `Pay Until`; the two fields the brief actually wants do not
    exist yet. Only after the tick do they appear — a date `Pane` named **`at`** (not
    `"payment date"`, which exists nowhere) and an `Edit` named **`Value`**, prefilled
    with the invoice total. They must therefore be resolved *after* the checkbox, never
    before. `Value` is confirmed rather than blindly overwritten, the same stance as
    `complete_line`'s U.Price check.

35. **A checkbox read straight after `toggle()` returns the OLD state.** `_check()` set
    the toggle then verified it with a single immediate read, and that read reported the
    *pre-toggle* value — so a toggle that had genuinely worked was reported as
    `wrote: True, read: 0`. It surfaced on the Invoice's `paid` box, where the click also
    triggers a panel re-layout (#34) and so lags further. Earlier step-by-step probing
    hid it, because the box was already ticked from a previous run — a good reminder
    that probing against dirty state can conceal exactly the bug you're hunting. Now
    polled to settle, per this codebase's no-fixed-sleeps rule (`uia/waits.py`).

### Round 4: found by running a second order through the CLI

36. **`reconcile()` rejected any order that charges shipping.** It compared the bare
    sum of line totals against `net_total`, which silently assumed `shipping_amount`
    and `order_discount_pct` were always zero — true of the brief's own sample, and
    false in general. A second test order (`data/golden/order_002.json`, shipping
    `20.00`) was therefore rejected with *"sum of line totals 940.00 != order
    net_total 960.00"* and a matching VAT complaint of `3.80` — a **correct document
    failing a check that simply didn't model it**, which is the failure mode most
    likely to be mistaken for bad extraction. Now discount and shipping are both part
    of the expected net. VAT on shipping is inferred only when every line shares one
    rate; where lines disagree the rate applying to shipping genuinely isn't in the
    document, so that is reported for a human rather than guessed.
37. **A successful `--dry-run` reported "unexpected error" and printed a traceback.**
    `typer.Exit(0)` — how typer signals a *normal* exit — was raised inside the `try`
    and swallowed by the generic `except Exception`, so a clean run announced itself
    as a crash while still exiting 0. Caught and re-raised ahead of the generic
    handler.

38. **The second delivery address is a mirrored column with duplicate labels.**
    Unchecking "Delivery Address equals Invoice Address" reveals a whole second
    column to the right carrying an *identical* label set — `Gender`,
    `First Name Last name`, `Company`, `Street`, `ZIP, City`, `Country`. Nothing but
    x-position separates the delivery fields from the billing ones, so label-anchored
    resolution alone would write delivery values into billing fields (or vice versa)
    with no error. `resolve_by_label` / `resolve_pair_by_label` now take
    `pick="rightmost" | "leftmost"`, and every delivery write pins the anchor
    explicitly. The billing fields are written *before* the uncheck, while only one
    column exists, so they can't be caught by the same ambiguity. This closes the last
    branch the project had refused to attempt.
39. **The order-level Shipping charge was never written.** Every item line was correct
    and yet all three totals came up short by exactly the shipping amount (`940.00` vs
    `960.00`, VAT `178.60` vs `182.40`) — because nothing ever told Fakturama about it.
    Shipping is an order-level field, not implied by any line. Now written and verified
    before the totals are read.
40. **Fakturama's "Total Net" excludes shipping; its "Total" includes it.** Live-
    confirmed on an order shipping at 20.00: `Total Net 940.00`, `VAT 182.40`,
    `Total 1142.40` — i.e. `940 + 20 + 182.40`. `SourceOrder.net_total` uses the other
    common convention and counts shipping *in*. Both are internally consistent; they
    simply mean different things by "net". The check now compares `Total Net` against
    the items net derived from the lines, and `Total` against `gross_total`, rather
    than forcing one convention onto the other.
41. **A tooltip aborted a perfectly good Save.** `check_for_new_dialog()` treated *any*
    new top-level window as a blocking dialog, and SWT renders tooltips as real
    top-level windows with an empty title — so a save that had nothing wrong with it
    failed with `Save triggered a dialog instead of completing: '<untitled>'`, pointing
    the operator at a dialog that had already vanished. Now a new window counts as a
    dialog only if it has a title *or* clickable buttons (a real SWT MessageBox always
    has one of those; a tooltip has neither). Deliberately still conservative the other
    way — an untitled window *with* buttons counts — because missing a real modal is
    what caused the original hang this detection exists to prevent.

42. **The resolver called a clear-cut case ambiguous, and the flow went silent while
    doing it.** Two separate defects surfaced on one real run of the sample image:

    *The false ambiguity.* Three live contacts shared a person and street — "Marta
    Klein, Friedrichstrasse 88" — but had plainly different companies ("Northstar
    Office GmbH", "... FreshTest", "... DupTest2"). The name+street mirror, which
    exists only to predict Fakturama's own duplicate popup, reported all three, and
    the run halted for manual review on a question whose answer was never in doubt.
    `resolve()` now consults **exact company name first** (case-insensitive,
    whitespace-trimmed, never partial): exactly one match is reused outright, more
    than one still goes to review, and no match falls through to the original rules
    with every check intact. Matching on company alone is safe specifically because
    the resolver only chooses the UI *search key* — `try_select_debtor` still gates
    the actual selection on all five fields against the real dialog rows.

    *The silence.* Between "extraction OK" and the first UI action the run printed
    nothing for up to three minutes while Fakturama cold-started — indistinguishable
    from a hang, and it caused an operator to kill a run that was working correctly
    (no `state.json` or `report.md` was written, the signature of a `KeyboardInterrupt`
    rather than a crash). `RunState.note()` now prints live, `run_flow` prints a
    header per phase, and `launch_or_attach` says whether it attached or is waiting
    on a cold start — and how long that takes. Progress that only appears after
    success is no help during the part where you actually need it.

43. **Cold start latched onto the splash window and waited three minutes for a toolbar
    that could never appear there.** `launch_or_attach()` resolved `main_window` ONCE,
    then polled that fixed reference for the toolbar. During startup Fakturama shows a
    splash/early window whose title also passes the "starts with fakturama" test, so
    the session bound to the splash while the real main window opened beside it
    unnoticed — and then spent the full 180s `startup_timeout` looking for a toolbar
    inside the wrong window. The failure was silent by construction: the lookup kept
    returning "not yet" rather than raising, so the error carried
    `last_exception: None` and said nothing about what was actually wrong. Reported by
    an operator as "it opens the app and then does nothing for 3 minutes".

    Fixed by making window-finding part of the readiness poll instead of a step before
    it, and by testing **every** candidate window rather than the first title match:
    the window that *has* the toolbar is by definition the real main window, so a
    splash can never satisfy the check. A 15-second heartbeat (`... still loading
    (45s)`) was added for the same reason as #42's live progress — a slow start should
    look like progress, not a hang. Verified against a genuine cold start, which now
    runs through all five phases.

    The general lesson, and it is the same one as #24: a check that can only answer
    "not yet" can never tell you it is asking the wrong question. Both bugs hid for
    the same reason — the verification was pointed at the wrong object.

44. **Every VAT rate that is a multiple of ten was created as 0%.** The worst bug in
    the project, and it hid for the entire build because the sample order happens to
    be 19%.

    `ensure_vat` typed `str(item.vat_pct.normalize())` into the TAX Rate editor's
    Value field. For 20 that is not `"20"` — `Decimal("20").normalize()` strips the
    trailing zero into an exponent, giving `Decimal('2E+1')` and the string
    `"2E+1"`. Fakturama parses that as **0**. The result was a tax rate *named*
    "VAT 20%" and *worth 0%*: the name matched every subsequent lookup, the product
    linked to it happily, and the only symptom appeared three steps later as
    `line VAT '0 %' does not match extracted 20%`. Confirmed in the database —
    `INSERT INTO VATS VALUES(3,'',FALSE,'VAT 20%','VAT 20%',0.0E0)`. 10%, 20% and
    30% were all affected; 19% and 7% were safe by arithmetic accident.

    What makes this one instructive is that the codebase **already knew**: 
    `vat_name()` carried a comment explaining this exact trap and used
    `format(..., "f")` to avoid it. The guard was written for the name and simply
    never applied to the value — the knowledge was present and the fix was not.
    Now shared: `SourceItem.vat_pct_str()` is the single formatter both use.

    Two further changes, because a wrong tax rate must not be able to reach a saved
    record again: the reuse check compares the Value **numerically** rather than by
    string (it was comparing against `"2E+1"` too, so it could never match and would
    have created a duplicate on every run), and `ensure_vat` now reads the Value back
    **before saving** and refuses to save a rate whose stored value isn't the one
    asked for. Seven parametrised regression tests cover 0/2.5/7/10/19/20/30.

45. **The retry logic couldn't catch the failure that mattered most: a hang.** The
    Gemini SDK's request timeout defaults to `None` — wait forever. Retrying was wired
    to fire on *exceptions* (503, 429), so a request that simply never came back never
    raised, never retried, and printed nothing. Reported as "why did this take so much
    time, and why didn't it even try?" — a fair description of a run sitting silent and
    indefinite.

    An explicit 90s per-request timeout (`FIC_GEMINI_TIMEOUT`) turns a hang into an
    ordinary transient failure that the existing backoff already handles, and
    `_is_transient` now also classifies timeouts and dropped connections as retryable —
    matching on the exception's class name too, because httpx timeout types can have an
    empty `str()` and would otherwise slip through. Verified live on the very first
    run after the change: `attempt 1/5 failed after 91s (ReadTimeout)` → retry →
    `image read in 63s`. Without it that attempt would still be hanging.

    The general point: **retry logic is only as good as its definition of failure.**
    Handling every error the API can *return* is worthless against the case where it
    returns nothing at all, and "no response" is the failure mode most likely to look
    like a broken program rather than a slow network.

46. **A model swap changed the extraction, and three weaknesses turned that into an
    unreadable failure.** Switching model (an availability workaround, not a code
    change) made the extraction return `first_name=None, last_name=None` for a debtor
    the previous model had read as "Elena Richter" — it still got
    `elena.richter@eurotech.test`, it just didn't split out the person. Everything
    after that was the system handling an honest input difference badly:

    - The failure message was *"the exact-match search and the just-written values
      disagree"* — true and useless. It knew which field differed and didn't say.
      Now every rejected row is reported per-field (`expected '' vs dialog shows
      'Elena'`), which turns a database-archaeology session into a five-second read.
    - The post-creation re-selection searched by the **resolver's** recommendation,
      which pointed at a *different, older* contact and returned its surname
      ("Richter"). The contact just created had no surname, so that search could never
      find it — re-selection was unwinnable by construction. It now searches by the
      company it just wrote: the value we are certain about.
    - `parse_contacts` read only `Database.script`, so a contact created moments
      earlier — including by the current run — was **invisible** to the resolver,
      which kept naming the older contact as the only match. It now also reads
      `Database.log`, HSQLDB's write-ahead file, where uncheckpointed rows live.

    Worth stating plainly: the extraction difference was not a bug, and no code change
    could have prevented it. What was wrong was that a legitimate input variation
    produced a diagnosis-resistant failure and a duplicate contact. **Model output is
    an input, and inputs vary** — the surrounding code has to stay legible when they do.

47. **It would create a duplicate customer while the database was telling it not to.**
    Raised by the user as a question -- *"when there is a contact you close it and
    create a new one?"* -- which was a sharper reading of the behaviour than the code
    had.

    The select-or-create logic itself is right: an exact five-field match selects the
    existing contact; no match cancels the dialog and creates one. What was missing was
    that the two sources of truth were never compared. The DB resolver would report
    *"exactly one match (id=10)"* for the company, the UI's five-field test would reject
    every row, and the flow would quietly create a second customer -- because nothing
    asked why those two answers differed. That is how a blank first/last name in one
    extraction (#46) produced a duplicate "EuroTech Solutions GmbH".

    A duplicate customer is among the worst outcomes this system can produce: it splits
    a real customer's invoice history across two records, and it is not something a
    later run can detect or undo. So the contradiction is now a halt, reported with
    both the stored contact and the per-field differences, and the message names the
    likely cause (a field the extraction dropped). The post-creation re-selection is
    exempt, since there the resolver is legitimately matching the contact just created.

    Worth stating: this was not a broken check, it was **two working checks that never
    spoke to each other**. Agreement between independent sources is itself a signal,
    and discarding it is how a system stays confidently wrong.

48. **The same column-order mistake, a third time -- and this one created a duplicate
    on every single run.** `ensure_payment_method` read the Payments list as
    `["Name"]`, but `read_table_rows` maps values left-to-right and that grid's first
    column is **Standard**. So a blank cell was compared against the payment method
    name, the existence check could never match, and every run created another
    "Bank Transfer". Spotted by the user from a screenshot showing two identical rows;
    the database confirmed four rows where there should have been two.

    This is the third instance of one mistake (#27 fixed it for VATs, #13 for the
    product dialog), which makes the pattern -- not the instance -- the actual defect:
    **hardcoding a column list is a guess that silently rots.** `locator.header_columns()`
    now asks the grid for its own captions, and the hardcoded list survives only as a
    fallback for a grid that exposes no headers. Guessing was never necessary; the
    information was in the UI the whole time.

49. **Idempotency: the run had no memory, and the database proved it.** Re-running the
    tool on the same image created a second Order and a second Invoice, and nothing
    anywhere noticed -- the run reported complete success both times, because by every
    check it applied it *had* succeeded. The error type and exit code for this had been
    defined from the start and never raised.

    Implementing it immediately showed the cost of not having it: one order reference
    (`WEB-2026-0829-C88`) already had **PO000016, INV000009, PO000017 and INV000010**
    against it -- two full orders for one purchase -- and another had three. That is the
    failure mode worth fearing here: not a crash, but a clean run that quietly bills a
    customer twice.

    A pre-flight now matches the saved documents' **CUSTOMERREF column** against the
    extracted reference before Fakturama is opened, and refuses with the document numbers
    it found. Deliberately not a substring search of the database file -- a reference
    string can also appear in an address or a note, and refusing to run for the wrong
    reason is its own bug. Deleted documents are ignored, the write-ahead log is included
    (the order from the *previous* run is usually only there), and repeated rows are
    deduplicated so one document cannot be reported as three. `--force` overrides it for
    the case where a duplicate is genuinely wanted.

**One unexplained event, recorded rather than smoothed over:** a five-phase run died
mid-invoice with no traceback and no `finally` output — the process simply vanished
after saving the Order and opening the Invoice. Re-running the identical code path
step by step completed cleanly, and it has not recurred. Most likely a transient fault
in the UIA/COM layer under load. It is not diagnosed, and saying otherwise would be
guessing.

**Two known soft gaps on the Contact editor's Miscellaneous tab**, both logged and
non-fatal by design rather than silently ignored: the `Alias name` label isn't found
in this build (`could not set Alias name`), and the `Discount` field rejects the
written value (`could not set Discount`). Neither is required by `SourceDebtor`
validation or gates the flow, and both are surfaced in the run log.

---

## What I skipped

- **The final `Save` click of Step 4, and all of Step 5.** Step 3 (product/VAT
  resolution *and* full line completion — Qty/U.Price/VAT/Discount/Price) is fully
  live-verified end to end, and Step 4's Total/Total Gross comparison is confirmed
  correct against a real order. What's left is the VAT reconciliation question (see
  bug #21) and actually clicking Save + verifying the saved Documents row, then the
  Invoice follow-up itself — implemented against the same proven primitives and real,
  `fic probe`-confirmed field names, but not driven that far live in the session's
  time.
- **The differing-delivery-address UI** (unchecking "Delivery Address equals Invoice
  Address" presumably reveals a second address block) — the checkbox itself is
  confirmed working; what it reveals when unchecked was never explored live. The code
  raises `ManualReviewRequired` rather than guessing at unconfirmed fields.
- **Email/Telephone/Alias field locations.** Not found on the Contact editor's
  "Address" or "Miscellaneous" sub-tabs in this build — genuinely absent from this
  simplified layout, or living somewhere not checked (a "Notice" tab exists and was
  never opened). Handled with graceful degradation (logged, not fatal) rather than a
  crash, since neither is required by the brief's validation rules.
- **OCR fallback extractor, Gemini as anything but the cross-check partner,
  `--resume`, the DB read-only oracle.** All named in the design docs as lower
  priority than the UI-automation core, and cut in that order as time ran out.
- **A proper `pytest -m e2e` suite against a resettable Fakturama workspace.** The
  live verification that did happen was done by directly driving flow functions during
  development, which found real bugs but isn't a repeatable regression suite.

## If I had 3 more hours

1. **Solve the VAT reconciliation question** (~30 min), then **finish Step 4's Save
   and push into Step 5** (~30 min) — now the actual highest-leverage remaining work,
   since Step 3 and the Total/Total Gross check are both solved. Start by manually
   entering the exact same order (4×SAMPL01, 10% discount) into Fakturama by hand and
   reading its own VAT calculation breakdown/tooltip if one exists, or checking
   `DocumentItemEditingSupport`'s VAT-column formula in the source used for the grid
   breakthrough — cheaper than more live trial and error. Then confirm the Invoice
   follow-up-document mechanism for real, including paid/payment-date/Value.
2. **Re-confirm the `save()` duplicate-dialog fix cleanly, in one continuous run**
   (~20 min). The fix (diffing top-level windows, guarding right after Street) is
   implemented and reasoned through carefully, and the underlying technique was
   proven earlier in development for a different dialog — but a clean, final,
   single-run confirmation of *this specific* fix didn't complete within this
   session's time budget.
3. **Build the `pytest -m e2e` suite** (~40 min) against a workspace-reset fixture,
   turning what was ad hoc live debugging into a repeatable regression suite — every
   bug in the list above (18 of them, several genuinely subtle) would make a good
   test case, and the grid activation sequence specifically deserves a pinned
   regression test given how many wrong turns it took to find.
4. **Explore the differing-delivery-address second-address UI** (~25 min) live, to
   replace the current `ManualReviewRequired` with an actual implementation.
5. **Independently re-confirm the DB contact-resolver wiring** (~15 min) inside
   `try_select_debtor()` — the resolver module itself is live-verified against the
   real database; the wiring that makes the UI search use its recommendation wasn't
   re-run live after being connected.
