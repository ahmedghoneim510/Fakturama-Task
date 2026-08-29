# Fakturama Image-to-Cash Automation — Engineering Spec (Python)

Version 1.0 · Target: Part 1 (design doc) + Part 2 (implementation) of the TJM take-home.

---

## 0. What the brief actually asks for

One continuous **Order-first** flow driven by a single order image:

```
order image ──▶ extract & normalize ──▶ open New Order (keep tab open the whole time)
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                  resolve Debtor    resolve Payment    resolve VAT + Product
                  (select → else     Method (select     (select → else create
                   create → back      → else create      → back to Order)
                   to Order)          → back)                 × N items
                          └─────────────────┼─────────────────┘
                                            ▼
                              complete Order → Save → verify in Data > Documents
                                            ▼
                       follow-up "Invoice" from the saved Order (NOT toolbar Invoice)
                                            ▼
                    set payment method + paid state → Save → verify both rows
```

Hard rules restated from the brief, because they are the grading surface:

| # | Rule | Consequence for the code |
|---|------|--------------------------|
| R1 | No hardcoded coordinates, no fixed UI layout | Every control is resolved at runtime from the accessibility tree or from vision; nothing is a magic `(x, y)` |
| R2 | The Order's own selectors are the **existence check** | Never query the DB or a cache to decide "does this Debtor exist" — open `Select the address` / `Select a product` and look |
| R3 | Master data is created **only** when exact selection fails | Creation is a fallback branch, never the default |
| R4 | The New Order tab stays open across every detour | The flow is a stack: push editor → resolve → pop back to the same Order |
| R5 | Ambiguous / conflicting results ⇒ **stop for manual review** | A first-class terminal outcome with its own exit code, not an exception swallowed by a retry |
| R6 | "Click Save **once**" | Save is guarded and idempotent-by-verification, never a retry loop |
| R7 | Invoice must come from the Order's *Create a follow-up document* area | Preserves the Order↔Invoice link; the toolbar button does not |
| R8 | Verify after every step before moving on | Read-back assertions are part of each action, not a final smoke test |
| R9 | Do not create Delivery / Correction / Dunning documents | Flow terminates hard after invoice verification |

### 0.1 Deliverables — the literal checklist from the brief

Both parts are graded on artifacts, not just on a working flow. Nothing below is optional, and none of it is allowed to fall off the end of the timebox.

| # | Deliverable | Where it comes from | Done when |
|---|---|---|---|
| D1 | **Part 1: design doc, 1–4 pages, no code, ≤90 min** | `docs/design.md` — a *trim* of this spec, not this spec (§13.0) | 4 pages or fewer, prose + diagrams only, covers grounding strategy · extraction strategy · tradeoffs |
| D2 | **Source code with a clear structure in a Git repo** | §2 layout; `git init` on the first commit, conventional history | `git log` shows incremental commits, not one dump |
| D3 | **Setup instructions: dependencies + how to run against Fakturama** | `README.md`, sourced from §1 and §10 | A reader with a clean Windows box can go from zero to `fic run` |
| D4 | **Annotated screenshots or a short recording** | `runs/<id>/screenshots/` + `report.md` (§10), curated into `docs/figures/` | One annotated image per flow stage (§8 Steps 1–5), referenced from the README |
| D5 | **README** incl. an explicit *"what I skipped"* section | §11, §14 | Every unimplemented branch named, with the reason |
| D6 | **Written question: "if you had 3 more hours"** | §15 | Answered in the README, not only in this spec |

D4 is the one that historically dies at the 5-hour mark. It does not need extra work at the end: `capture.py` writes a screenshot on every verified action during the run, so producing D4 is *selecting* from `runs/<id>/screenshots/`, not staging new ones. Budget 10 minutes for the selection, not an hour for the capture.

---

## 1. Environment & hard prerequisites

**This is Windows-only work.** Microsoft UI Automation is a Win32 API; the current dev box is Linux. Before any code is written:

- Windows 10/11 VM or host (VirtualBox / VMware / a spare machine). Give it a **fixed resolution** (1920×1080) and **100% display scaling** — not because we hardcode coordinates, but because vision fallback and screenshot artifacts are far easier to reason about with a stable canvas.
- Fakturama 2.x from <https://www.fakturama.info/download/> (bundles its own JRE). First launch creates a workspace, by default under `%USERPROFILE%\Fakturama2\` with an embedded database directory — confirm the exact path on first run and record it in `config/app.yaml`.
- Python 3.12 (Windows build), `py -3.12 -m venv .venv`.
- Tesseract OCR (UB-Mannheim Windows build) with `eng` + `deu` traineddata, on `PATH`, for the OCR fallback path.
- `ANTHROPIC_API_KEY` in the environment (or `ant auth login`) for the vision extractor.

**DPI awareness is mandatory.** Call this before touching UIA or taking a screenshot, or UIA bounding rectangles and screen pixels will disagree on a scaled display:

```python
import ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
```

### Dependencies

```toml
# pyproject.toml [project.dependencies]
"anthropic>=1.0",        # vision extraction
"pydantic>=2.7",         # schemas + validation
"pywinauto>=0.6.8",      # UIA backend, high-level control wrappers
"uiautomation>=2.0.20",  # raw UIA tree walking where pywinauto is too opinionated
"comtypes>=1.4",
"pillow>=10",
"mss>=9",                # fast window screenshots
"pytesseract>=0.3.10",   # OCR fallback + grid reading
"typer>=0.12",           # CLI
"rich>=13",              # console run log
"structlog>=24",         # JSONL trace
"pyyaml>=6",
"python-dateutil>=2.9",
# dev: pytest, pytest-mock, ruff, mypy
```

---

## 2. Repository layout

```
fakturama-image-to-cash/
├── README.md                    # setup + run + "what I skipped"
├── pyproject.toml
├── docs/
│   ├── design.md                # D1: Part 1 deliverable — ≤4 pages, prose, no code
│   ├── sample_order.png         # the source image from the brief
│   └── figures/                 # annotated screenshots for the deliverable
├── config/
│   ├── app.yaml                 # exe path, workspace path, timeouts, locale
│   └── selectors.yaml           # semantic control map — NO coordinates
├── data/
│   ├── input/order_001.png
│   └── golden/order_001.json    # expected extraction, used as a unit-test fixture
├── runs/                        # per-run artifacts (gitignored except .gitkeep)
├── src/fic/
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── errors.py                # ManualReviewRequired, ControlNotFound, VerificationFailed
│   ├── logging_setup.py
│   ├── models/
│   │   ├── source.py            # SourceOrder, SourceItem, SourceDebtor, SourcePayment
│   │   └── state.py             # RunState (resumable)
│   ├── extraction/
│   │   ├── base.py              # Extractor protocol
│   │   ├── vision.py            # Claude vision → strict JSON
│   │   ├── ocr.py               # tesseract fallback
│   │   ├── normalize.py         # dates, decimals, phone, country, casing
│   │   └── reconcile.py         # arithmetic self-check
│   ├── uia/
│   │   ├── session.py           # launch/attach, main window handle, tab management
│   │   ├── snapshot.py          # UIA subtree → serializable node list
│   │   ├── locator.py           # the grounding engine (§5)
│   │   ├── anchors.py           # label→field spatial resolution
│   │   ├── vision_locate.py     # OCR/LLM screen-grounding fallback
│   │   ├── waits.py             # wait_until, wait_stable, wait_modal
│   │   ├── actions.py           # click, set_text, select_combo, save — all verified
│   │   ├── grid.py              # item-table strategy (canvas-safe)
│   │   └── capture.py           # screenshots + annotation
│   ├── flows/
│   │   ├── orchestrator.py      # the state machine
│   │   ├── s1_order_open.py
│   │   ├── s2_debtor.py
│   │   ├── s2a_payment_method.py
│   │   ├── s3_vat.py
│   │   ├── s3_product.py
│   │   ├── s4_order_complete.py
│   │   └── s5_invoice.py
│   └── report/
│       ├── trace.py             # JSONL action log
│       └── render.py            # runs/<id>/report.md
└── tests/
    ├── unit/                    # normalize, reconcile, gross-price math
    ├── contract/                # locator engine vs. recorded UIA snapshots
    └── e2e/                     # full flow against a reset Fakturama workspace
```

---

## 3. Domain model

Money is `Decimal` end to end. Never `float`. Serialize as strings.

```python
# src/fic/models/source.py
from decimal import Decimal
from datetime import date
from typing import Literal, Optional
from pydantic import BaseModel, Field

Money = Decimal

class SourceAddress(BaseModel):
    name: str                      # "Northstar Office GmbH" / "Northstar Office Warehouse"
    street: str
    zip: str
    city: str
    country: str                   # normalized to Fakturama's country list, e.g. "Germany"

class SourceDebtor(BaseModel):
    company: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    alias: Optional[str] = None            # → Miscellaneous > Alias name
    email: Optional[str] = None
    phone: Optional[str] = None
    customer_id_hint: Optional[str] = None # from image; NOT written (R: keep proposed ID)
    billing: SourceAddress
    delivery: Optional[SourceAddress] = None
    @property
    def delivery_same_as_billing(self) -> bool: ...

class SourcePayment(BaseModel):
    method: str                            # verbatim: "Bank Transfer"
    paid_status: Literal["PAID", "UNPAID", "PARTIAL"]
    payment_date: Optional[date] = None

class SourceItem(BaseModel):
    position: int
    sku: str                               # "CHR-ERG-01"
    description: str
    quantity: Decimal
    unit: Optional[str] = None
    unit_net_price: Money
    discount_pct: Decimal = Decimal("0")   # 10 means 10%
    vat_pct: Decimal                       # 19 means 19%
    line_net_total: Money                  # as printed on the source

    def expected_line_net(self) -> Money:
        return q2(self.quantity * self.unit_net_price
                  * (Decimal(1) - self.discount_pct / 100))

    def product_master_gross(self) -> Money:
        # Brief §3.9: master price ignores the transaction-line discount
        return q2(self.unit_net_price * (Decimal(1) + self.vat_pct / 100))

class SourceOrder(BaseModel):
    external_reference: str                # → Cust.Ref.
    order_date: date
    currency: str = "EUR"
    debtor: SourceDebtor
    payment: SourcePayment
    items: list[SourceItem] = Field(min_length=1)
    net_total: Money
    vat_total: Money
    gross_total: Money
    confidence: dict[str, float] = {}      # per-field, from the extractor
```

`q2(x)` = `x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`.

### Golden fixture — the sample image in the brief

`data/golden/order_001.json` encodes exactly this, and `tests/unit/test_golden.py` asserts the extractor reproduces it:

| Field | Value |
|---|---|
| External Reference | `WEB-2026-0714-A17` |
| Order Date | `2026-07-14` |
| Currency | EUR |
| Company / Contact / Alias | Northstar Office GmbH / Marta Klein / `NORTHSTAR-BERLIN` |
| Email / Phone | `marta.klein@example.test` / `+49 30 5550 1420` |
| Billing | Northstar Office GmbH, Friedrichstrasse 88, 10117 Berlin, Germany |
| Delivery | Northstar Office **Warehouse**, Beusselstrasse 44, 10553 Berlin, Germany |
| Payment | Bank Transfer · PAID · `2026-07-18` |
| Item 1 | `CHR-ERG-01` · Ergonomic Desk Chair · 2 pcs · 250.00 net · 10% disc · 19% VAT · line 450.00 |
| Item 2 | `MAT-DESK-02` · Anti-Fatigue Desk Mat · 3 pcs · 40.00 net · 0% disc · 19% VAT · line 120.00 |
| Totals | Net 570.00 · VAT 108.30 · Gross 678.30 |

Two derived values the flow must produce and that are easy to get wrong:

- Product master gross prices: `250.00 × 1.19 = 297.50` and `40.00 × 1.19 = 47.60` (discount **not** applied).
- Invoice payment `Value` when PAID: the **full Invoice Total**, `678.30`.

**Trap worth calling out:** the brief's §2.8 happy path says "if billing and delivery are identical, also assign the Delivery address role and do not create another address." In this sample they are **not** identical — different recipient name, street, ZIP. So the Debtor-creation branch must add a **second** address (Beusselstrasse 44 / 10553 Berlin) carrying the **Delivery address** role, while Main address keeps only **Invoice address**. Code must branch on `delivery_same_as_billing`, not assume the simple case.

---

## 4. Extraction subsystem

### 4.1 Primary: Claude vision with a strict schema

Single call, structured output, no free-form parsing. Adaptive thinking on — this is a dense document with arithmetic that must reconcile.

```python
# src/fic/extraction/vision.py
import base64, anthropic
from fic.models.source import SourceOrder

MODEL = "claude-opus-5"

SYSTEM = """You extract structured data from purchase-order images.
Rules:
- Transcribe values verbatim. Never infer, complete, or correct a value that is not printed.
- Money and quantities: digits only, '.' decimal separator, no currency symbol.
- Percentages: numeric only ("19", not "19%").
- Dates: ISO 8601 (YYYY-MM-DD).
- If a field is absent from the image, return null. Do not guess.
- Report per-field confidence in 0.0-1.0 for any value you are not certain you read correctly."""

def extract(image_path: str) -> SourceOrder:
    data = base64.standard_b64encode(open(image_path, "rb").read()).decode()
    resp = anthropic.Anthropic().messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": data}},
            {"type": "text", "text":
             "Extract the complete order. Include every item row, both addresses "
             "(billing and delivery separately, even if they look similar), the payment "
             "block, and the printed totals."},
        ]}],
        output_format=SourceOrder,
    )
    return resp.parsed_output
```

Notes that matter:
- `messages.parse(..., output_format=Model)` returns a **validated** `SourceOrder` in `resp.parsed_output` — no `json.loads`, no repair prompt.
- `budget_tokens` is rejected on Opus 5; use `thinking={"type": "adaptive"}` + `output_config={"effort": ...}`.
- Model ID is `claude-opus-5` exactly — no date suffix.
- For large images prefer `client.messages.stream(...)` + `.get_final_message()`; a single page PNG is fine non-streaming.

### 4.2 Fallback: OCR (`--extractor ocr`)

`pytesseract.image_to_data(..., output_type=DICT)` gives word boxes. Reconstruct the items table by y-band clustering (words within ±6 px of a common baseline form a row), then assign columns by x-overlap with the header words `SKU / Description / Qty / Unit net / Disc. / VAT / Line net`. Labelled scalars (`EXTERNAL REFERENCE`, `ORDER DATE`, …) resolve by "nearest text block below the label word within the same column band". This exists so the project runs offline and so the design shows a non-LLM path; it is expected to be the weaker of the two.

### 4.3 Normalization (`normalize.py`)

| Input shape | Normalized |
|---|---|
| `2026-07-14`, `14.07.2026`, `07/14/2026` | `date(2026,7,14)` — `dateutil` with `dayfirst` chosen by detected document locale |
| `1.234,56` / `1,234.56` / `250.00` | `Decimal("1234.56")` — decide separator by last-separator position |
| `10%`, `10 %`, `10` | `Decimal("10")` |
| `DE`, `Deutschland`, `Germany` | `"Germany"` — mapped against Fakturama's country combo values, read live from the UI once and cached |
| `+49 30 5550 1420` | kept verbatim (Fakturama does not normalize phones) |

### 4.4 Reconciliation gate (`reconcile.py`)

Runs before a single click happens. Any failure ⇒ `ManualReviewRequired`, exit 2, nothing touched.

1. Per line: `expected_line_net() == line_net_total`.
2. `sum(line_net_total) == net_total`.
3. `sum(q2(line_net * vat_pct/100)) == vat_total` (per-line VAT then sum — matches how Fakturama accumulates).
4. `net_total + vat_total == gross_total`.
5. `payment_date is not None` iff `paid_status == "PAID"`.
6. Every item has a non-empty `sku` and `vat_pct is not None`.
7. Any field with `confidence < 0.80` is listed in the review reason.

On mismatch with the vision extractor, do **one** re-ask that includes the specific discrepancy ("line 1 computes to 450.00 but you reported 460.00; re-read row 1"). One retry, then stop. Never silently "fix" the arithmetic — a wrong OCR read that we correct to a plausible number is the worst possible failure mode here.

### 4.5 Input preflight — the brief says "scanned or photographed"

Our sample is a clean synthetic PNG, but the brief describes the input as *"for example, a scanned or photographed purchase order"*, so the pipeline must not pretend every input is pristine. We do not implement deskew or perspective correction (§14 states that plainly as a limitation); what we do implement is **detection**, so a degraded image fails loudly at the door instead of producing plausible-but-wrong numbers deep in the flow:

| Check | Method | On failure |
|---|---|---|
| Resolution floor | shorter edge ≥ 1000 px | warn; record in the report |
| Skew | Hough / minimum-area-rect on the binarized text mask; \|angle\| ≤ 1.5° | warn above 1.5°, `ManualReviewRequired` above 5° |
| Focus / contrast | variance of Laplacian below threshold, or OCR mean word-confidence < 60 | `ManualReviewRequired("illegible source")` |
| Multi-page | more than one page in a PDF input | `ManualReviewRequired` — out of scope, stated in the README |

This is ~20 lines of Pillow/OpenCV and it converts the single most dangerous silent-failure mode (a bad photo read confidently) into an exit code 2 with the offending image attached. It runs before §4.1, so a rejected image costs no API call and touches no UI.

---

## 5. The grounding engine — how controls get found

This is the core of the assessment. Fakturama is Eclipse RCP/SWT, so most widgets are native Win32 and visible to UIA, but names are inconsistent and `AutomationId` is usually absent. The engine is a **cascade**: each strategy is tried in order, and the one that succeeded is recorded in the trace, so the run report shows exactly how much of the flow needed vision.

### Strategy cascade

| # | Strategy | When it wins | Cost |
|---|----------|--------------|------|
| S0 | `AutomationId` / `Name` + `ControlType` within a scoped container | Toolbar buttons, dialog OK/Cancel, tree items in the left navigator | ~ms |
| S1 | **Label-anchored spatial resolution** | Form fields (`Cust.Ref.`, `ZIP`, `Alias name`, `Price gross`) | ~ms |
| S2 | Ordinal within a resolved group | Repeated identical rows (address role checkboxes) | ~ms |
| S3 | Keyboard traversal (`Tab` order from a known anchor, mnemonics, `Ctrl+S`) | Cells inside custom-drawn widgets | ~10ms |
| S4 | **Vision fallback** — screenshot + OCR word boxes, or a Claude vision call asked for a bounding box | Canvas-drawn grids, icon-only buttons with no accessible name | 0.5–3s |

Everything is scoped: `session.editor("New Order")` returns the pane for the open editor tab, and every lookup runs inside that subtree. This is what makes "keep the Order tab open" mechanically enforceable — a locator that resolves outside its declared scope raises rather than clicking the wrong tab.

### S1 in detail — label-anchored resolution

The whole point of R1: positions are **computed from the live tree**, never written down.

```python
def resolve_by_label(scope, label: str, want=("Edit", "ComboBox", "CheckBox"),
                     direction="right"):
    nodes = snapshot(scope)                       # (type, name, id, rect, enabled, value)
    anchor = best_text_match(nodes, label)        # normalized: lower, strip ':', collapse ws,
                                                  # de/en alias table ("Cust.Ref." | "Kundennr.")
    if anchor is None:
        raise ControlNotFound(label)

    cands = []
    for n in nodes:
        if n.type not in want or not n.enabled:
            continue
        if direction == "right":
            if n.rect.left < anchor.rect.right - 2:      # must start at/after the label
                continue
            overlap = vertical_overlap_ratio(n.rect, anchor.rect)
            if overlap < 0.5:                             # same visual row
                continue
            cands.append((n.rect.left - anchor.rect.right, n))
        else:  # "below" — stacked forms
            if n.rect.top < anchor.rect.bottom - 2:
                continue
            if horizontal_overlap_ratio(n.rect, anchor.rect) < 0.3:
                continue
            cands.append((n.rect.top - anchor.rect.bottom, n))

    cands.sort(key=lambda c: c[0])
    if not cands:
        raise ControlNotFound(label)
    if len(cands) > 1 and cands[1][0] - cands[0][0] < AMBIGUITY_MARGIN_PX:
        raise AmbiguousControl(label, [c[1] for c in cands[:3]])   # → manual review
    return cands[0][1]
```

`AMBIGUITY_MARGIN_PX` (default 12) is the safety valve: if two candidates are nearly equidistant from the label, we refuse rather than coin-flip. That refusal is a *feature* — it maps directly onto R5.

### S4 in detail — vision fallback

Used for controls with no accessible name. The brief names two explicitly: "the **upper** existing-contact icon beside Addresses (not the lower green +)" and "the **upper** Product-selection icon beside the Items table (not the green +)". These are icon-only SWT toolbar items, and if UIA gives them no `Name` we cannot pick "upper" by name.

Resolution without hardcoding:
1. Resolve the *labelled* neighbour that does have a name (the `Addresses` static text, or the Items table header) via S1.
2. Enumerate all clickable nodes whose rect is within a bounded neighbourhood of that anchor.
3. Sort by `rect.top`. "Upper icon" = index 0, "lower green +" = index 1. **Both are runtime-derived from the anchor**, not written coordinates.
4. If step 2 yields fewer than 2 candidates, escalate to the screenshot path: crop the anchor neighbourhood, send it to Claude vision asking for the bounding box of the described control, click the returned box's centre, then **verify the expected dialog appeared** — if the wrong dialog opens (a New Debtor editor instead of `Select the address`), close it and raise.

The verify-after-click step is what makes S4 safe. We never trust a vision-derived click; we trust the dialog that follows it.

### Caching

Resolutions are memoized per `(editor_identity, label, strategy)` for the lifetime of a run, and invalidated whenever the editor's tab set or the active modal changes. Cheap, and it keeps the trace readable.

---

## 6. Interaction primitives — every action is verified

```python
# src/fic/uia/actions.py
def set_text(ctrl, value: str, *, readback=True):
    ctrl.set_focus()
    send_keys("^a{DELETE}")          # select-all + delete beats .set_text() on SWT
    type_text(value)
    send_keys("{TAB}")               # commit; many SWT fields validate on focus-out
    if readback and normalize(read_value(ctrl)) != normalize(value):
        raise VerificationFailed(field=ctrl.label, wrote=value, read=read_value(ctrl))

def select_combo(ctrl, value: str):
    ctrl.expand()
    items = wait_until(lambda: ctrl.items(), timeout=5)
    match = exact_ci(items, value)   # EXACT match only — no fuzzy fallback
    if match is None:
        ctrl.collapse()
        raise OptionUnavailable(ctrl.label, value, items)   # → triggers create-branch or review
    match.select()
    assert_eq(read_value(ctrl), value)

def save(editor):
    before = editor.dirty_marker()   # SWT marks dirty tabs with a leading '*'
    click(resolve_toolbar(editor, "Save"))            # exactly once — R6
    wait_until(lambda: not editor.is_dirty(), timeout=15)
    if editor.is_dirty():
        raise VerificationFailed("save did not clear the dirty marker")
```

No `time.sleep()` anywhere. Waiting is expressed as conditions:

```python
wait_until(pred, timeout, poll=0.15)                 # predicate becomes true
wait_stable(fn, stable_for=0.6, timeout=10)          # value unchanged across N polls
wait_modal(title_pattern, timeout=10)                # dialog present + enabled
wait_modal_gone(handle, timeout=10)                  # dialog dismissed
```

`wait_stable` is what implements the brief's "wait for the list to stabilize" in the address and product selectors — poll the row count until it stops changing, not a fixed sleep.

---

## 7. The item grid — the highest-risk component

The Order's Items table in Fakturama is very likely a **custom-drawn grid** (Nebula/NatTable-style canvas), which means it may expose **zero UIA children** — no rows, no cells, nothing to click by name. Plan for that.

**Probe first, at runtime.** On first entry to §3, snapshot the items region and count `DataItem`/`Custom` descendants:

- **Accessible path** (children exist): use them. Resolve cells by column-header anchor + row index.
- **Canvas path** (no children): keyboard protocol only.
  1. The product selector is the entry point — after `OK` in `Select a product`, Fakturama populates the next line and puts focus in it.
  2. From that focus, traverse with `Tab` / `Shift+Tab`. When a cell enters edit mode SWT creates a **real `Edit` widget** as a child of the canvas — transient, but it *is* in the UIA tree while active. That gives us a legitimate read-back for `Qty.`, `U.Price`, `VAT`, `Discount`, `Price`.
  3. Learn the tab order once per run by walking it and recording which column header each stop sits under (compare the transient editor's rect x-range against the header rects). Store it in the run state; reuse for every subsequent line.
  4. If a cell's editor never appears, fall back to OCR of the row band for read-back — write blind, verify by picture.

**The totals block is the ultimate cross-check.** Whatever happened inside the grid, `Total Net` / `VAT` / `Total` are ordinary labelled fields resolvable by S1. After all lines are entered, assert they equal `net_total`, `vat_total`, `gross_total` from the source (brief §4.3). If the grid interaction silently corrupted a line, this catches it before Save.

---

## 8. Flow specification

State machine in `flows/orchestrator.py`. Each state is a pure step: it receives `(session, source, state)`, performs its work, verifies, appends to the trace, screenshots, and returns the next state. `RunState` is serialized to `runs/<id>/state.json` after every transition so `--resume` can pick up mid-flow.

```
EXTRACT → RECONCILE → OPEN_ORDER → SET_ORDER_HEADER
        → RESOLVE_DEBTOR ─(miss)→ CREATE_DEBTOR ─(payment miss)→ CREATE_PAYMENT_METHOD ─┐
        ←──────────────────────── RESELECT_DEBTOR ←─────────────────────────────────────┘
        → for each item: RESOLVE_PRODUCT ─(miss)→ ENSURE_VAT → CREATE_PRODUCT → RESELECT_PRODUCT
                       → COMPLETE_LINE
        → VERIFY_ORDER_TOTALS → SAVE_ORDER → VERIFY_ORDER_ROW
        → FOLLOWUP_INVOICE → VERIFY_INVOICE_COPY → SET_INVOICE_PAYMENT → SAVE_INVOICE
        → VERIFY_FINAL → DONE
```

### Step 1 — Open the Order (brief §1.3–1.8)

| Action | Locator strategy | Verification |
|---|---|---|
| Click `Order` in the top toolbar | S0 by `Name="Order"`, `ControlType=Button`, scoped to the main toolbar | `wait_until` a new editor tab whose title matches `New Order` / `Order` becomes active |
| Leave `No.` unchanged | — | Read and record the proposed number into `state.order_no` (needed for §4.5 verification) |
| `Date` ← order_date | S1 label `Date` | read-back, format-normalized |
| `Cust.Ref.` ← external_reference | S1 label `Cust.Ref.` | read-back exact string |
| price mode → `Net`, VAT → `With VAT` | S1 label + `select_combo` (exact) | read-back |

`state.order_editor_ref` is captured here and asserted still-open at the top of every later step (R4).

### Step 2 — Debtor (brief §2)

```
click upper contact icon (S4 upper-of-two beside "Addresses")
  → wait_modal("Select the address")
  → type source.debtor.company into the dialog's Search field (S1 label "Search")
  → wait_stable(row_count)
  → rows = read_grid(dialog)                     # this grid IS accessible (see figure 1)
  → exact = [r for r in rows if match_all(r, company, first_name, last_name, zip, city)]
```

Match rule is **exact, case-insensitive, whitespace-collapsed** on all five of Company / First Name / Name / ZIP / City. Then:

| Outcome | Action |
|---|---|
| exactly 1 exact row | select → `OK` → verify Invoice & Delivery address blocks in the Order match the source (§2.4) → continue to products |
| >1 exact row, **or** ≥1 row that matches company+city but conflicts on ZIP/name | `Cancel` → `ManualReviewRequired("ambiguous debtor", rows)` |
| 0 exact rows | `Cancel` → creation branch |

**Creation branch** — `New Contact` in the left `New` panel (S0 by name), Order tab stays open:

- Customer ID: **read and leave**. The image's `CUST-1007` is deliberately not written (§2.6).
- Company / First Name / Last Name; Salutation stays `---` when the source has none.
- `Addresses > Main address`: Street, ZIP, City, Country, E-Mail, Telephone from the billing address. The brief (§2.7) additionally names three optional fields — **additional name**, **Address specification**, **district** — which are filled *only* when the source supplies them. Our sample supplies none, so all three stay untouched. General rule: skip any field the source does not supply; never write an empty string into an optional field, because a written-then-blanked field is indistinguishable from a deliberate blank on read-back.
- **Role assignment (§2.8, branch-aware):**
  - `delivery_same_as_billing` → Main address gets both `Invoice address` and `Delivery address`; no second address.
  - otherwise (our sample) → Main address gets `Invoice address` only; add a second address for the delivery recipient and give it `Delivery address`. The differing recipient name (`Northstar Office Warehouse`) goes in the second address's name field.
- `Miscellaneous`: `Alias name` ← alias, `Discount` ← `0%`, `Net or Gross` ← `Net`.
- `Payment` tab: `select_combo(payment_method, exact)`. On `OptionUnavailable` → payment-method sub-branch below. Debtor editor stays open throughout.
- `save(debtor_editor)` — once.
- Return to the Order, reopen `Select the address`, search, select, `OK`. **Successful selection from the Order is the proof the Debtor saved** (§2.13) — no DB peek, per R2.

**Payment-method sub-branch (§2.10.1–2.10.6):** navigate `Data > terms of payment` in the left navigator (S0 tree item), search exact. One unambiguous exact row → go back and select it. Multiple/conflicting → manual review. None → green `+` at the upper-right of the list (S4: the *only* toolbar button in the list's header strip, resolved relative to the list rect), then:

| Field | Value |
|---|---|
| Name, Description | the exact extracted method (`Bank Transfer`) |
| Account | blank |
| payment-code dropdown | `Bank Transfer → Credit transfer`, `Credit Card → Credit card`, `SEPA Direct Debit → SEPA direct debit` (a literal, closed mapping table in `config/app.yaml`; an unmapped method ⇒ manual review) |
| Cash discount / Discount Days / Net Days | `0` |
| Text 'unpaid' / 'deposit' / 'paid' | blank |
| `Set as standard` | **not clicked** |

Save once → back to the Debtor editor → select the new method.

### Step 3 — Products (brief §3), per item, in source order

```
click upper Product-selection icon (S4, beside the Items table)
  → wait_modal("Select a product")
  → search the exact SKU
  → wait_stable(rows)
  → exact = rows where Item Number == sku (exact, ci)
```

1 exact → select, `OK`. Conflicting → manual review. 0 → `Cancel`, then:

**VAT first (§3.4–3.6), before `New product`.** `Data > VATs`, search `VAT {pct}%`. Reuse only if **all three** hold: `Name == "VAT {pct}%"`, `Value == pct`, `VAT code (E-Invoice) == "S (Standard rate)"`. Any conflict ⇒ manual review (do not edit an existing VAT row). Missing ⇒ green `+`, set Name/Description to `VAT {pct}%`, keep code `S (Standard rate)`, `Value` ← pct, leave `Standard VAT` untouched, save once.

**Then `New product` (§3.7–3.11):**

| Field | Value | Source |
|---|---|---|
| Item Number | `CHR-ERG-01` | sku |
| Name | `Ergonomic Desk Chair` | description |
| Description | `Ergonomic Desk Chair` | description (same value, per §3.8) |
| Price (gross) | `297.50` | `q2(unit_net × (1 + vat/100))` — **discount not applied** (§3.9) |
| cost price (net) | `0.00` | fixed |
| VAT | `VAT 19%` | exact select |
| Stock | `0.00` | fixed |
| Category, GTIN, supplier code, allowance, Product Picture, user defined field 1 | untouched | §3.10 |

Save once → back to the Order → reopen `Select a product` → search the SKU → select → `OK`. Not found ⇒ manual review (§3.12).

**Complete the line (§3.13–3.16):** `Qty.` ← quantity; assert/set `U.Price` == unit_net_price and `VAT` == vat_pct; `Discount` ← discount_pct; then assert line `Price` == `expected_line_net()`. Grid access per §7 above.

### Step 4 — Complete & save the Order (brief §4)

Re-verify addresses and all lines against `source`. Assert overall `Discount` is `0%` and `Shipping` is `Free of shipping costs / 0.00` (the sample supplies no order-level values). Assert `Total Net == 570.00`, `VAT == 108.30`, `Total == 678.30`. `save()` once. Then `Data > Documents` and confirm exactly one Order row with `state.order_no`, Date `2026-07-14`, Cust.Ref. `WEB-2026-0714-A17`, state `open`, Total `678.30`.

Then — **critically** — the Invoice comes from the saved Order's `Create a follow-up document` area, `Invoice` button (figure 8), resolved by S1 anchored on the `Create a follow-up document` group label. **Not** the toolbar `Invoice` button (R7). Wait for the linked `New Invoice` editor.

### Step 5 — Invoice (brief §5)

Leave Invoice No., Invoice Date, Service date at their proposed values. Verify the copied fields: Cust.Ref., Invoice address, Delivery address, Order Date, VAT mode, every item line, totals. Set/confirm the Invoice payment method == `Bank Transfer`; unavailable ⇒ manual review (§5.2 — no creation branch here).

Paid status is PAID ⇒ tick `paid`, set payment date `2026-07-18`, set `Value` `678.30` (full Invoice Total). Not PAID ⇒ leave `paid` clear, invent nothing (§5.3). Save once. `Data > Documents`: Invoice row has the expected state and Total, **and** the source Order row is still `open` with the same Cust.Ref. and Total (§5.5). Optionally reopen the Invoice to confirm persisted payment method / paid state / date / value (§5.6). **Stop.** No Delivery, Correction, or Dunning (§5.7, R9).

---

## 9. Configuration — `config/selectors.yaml`

Semantic descriptors only. Zero coordinates. This file is the readable contract between the flow code and the UI, and it's what makes the "no fixed layout" claim inspectable.

```yaml
main_window:
  title_re: "^Fakturama.*"

toolbar:
  order:   {strategy: name, control_type: Button, name: "Order"}
  save:    {strategy: name, control_type: Button, name: "Save"}

navigator:
  documents:        {strategy: tree_path, path: ["Data", "Documents"]}
  terms_of_payment: {strategy: tree_path, path: ["Data", "terms of payment"]}
  vats:             {strategy: tree_path, path: ["Data", "VATs"]}
  new_contact:      {strategy: name, name: "New Contact"}
  new_product:      {strategy: name, name: "New product"}

# --- brief §1.3-1.8 -----------------------------------------------------
order_editor:
  no:        {strategy: label, label: "No.",        want: [Edit], readonly_intent: true}
  date:      {strategy: label, label: "Date",       aliases: ["Datum"], want: [Edit]}
  cust_ref:  {strategy: label, label: "Cust.Ref.",  aliases: ["Kundennr."], want: [Edit]}
  net_gross: {strategy: label, label: "Net",        want: [ComboBox, RadioButton]}
  vat_mode:  {strategy: label, label: "VAT",        want: [ComboBox], expect: "With VAT",
              scope: header}                        # disambiguated from the totals "VAT"
  discount:  {strategy: label, label: "Discount",   want: [Edit], scope: totals}
  shipping:  {strategy: label, label: "Shipping",   want: [ComboBox, Edit], scope: totals}
  total_net: {strategy: label, label: "Total Net",  want: [Edit, Text], readonly: true}
  vat_total: {strategy: label, label: "VAT",        want: [Edit, Text], readonly: true,
              scope: totals}
  total:     {strategy: label, label: "Total",      want: [Edit, Text], readonly: true}
  invoice_address_block:  {strategy: label, label: "Invoice address",  want: [Text, Edit]}
  delivery_address_block: {strategy: label, label: "Delivery address", want: [Text, Edit]}
  select_contact_icon:
    strategy: anchored_icon
    anchor:   {strategy: label, label: "Addresses"}
    pick:     first_by_top          # upper icon; the green + is second
    verify_opens: "Select the address"
  select_product_icon:
    strategy: anchored_icon
    anchor:   {strategy: table_header, header: "Item Number"}
    pick:     first_by_top
    verify_opens: "Select a product"
  followup_invoice:
    strategy: label_scoped_button
    group:    "Create a follow-up document"
    name:     "Invoice"

# --- brief §3.13-3.16: line completion; see §7 for the canvas fallback ---
item_grid:
  region:  {strategy: anchored_region, anchor: {strategy: label, label: "Items"}}
  columns: {strategy: table_header,
            headers: ["Pos.", "Item Number", "Name", "Qty.", "U.Price",
                      "Discount", "VAT", "Price"]}
  cell:    {strategy: header_x_overlap_plus_row_index}   # no ordinals written down

# --- brief §2.5-2.11 ----------------------------------------------------
debtor_editor:
  customer_id:  {strategy: label, label: "Customer ID", want: [Edit], readonly_intent: true}
  company:      {strategy: label, label: "Company",     want: [Edit]}
  salutation:   {strategy: label, label: "Salutation",  want: [ComboBox], expect: "---"}
  first_name:   {strategy: label, label: "First Name",  want: [Edit]}
  last_name:    {strategy: label, label: "Name",        aliases: ["Last Name"], want: [Edit]}
  tabs:         {strategy: tab_item, names: ["Addresses", "Miscellaneous", "Payment"]}
  address:                                   # resolved inside the active address sub-tab
    street:      {strategy: label, label: "Street",      want: [Edit]}
    zip:         {strategy: label, label: "ZIP",         want: [Edit]}
    city:        {strategy: label, label: "City",        want: [Edit]}
    country:     {strategy: label, label: "Country",     want: [ComboBox]}
    email:       {strategy: label, label: "E-Mail",      want: [Edit]}
    telephone:   {strategy: label, label: "Telephone",   want: [Edit]}
    additional_name:       {strategy: label, label: "additional name",       optional: true}
    address_specification: {strategy: label, label: "Address specification", optional: true}
    district:              {strategy: label, label: "district",              optional: true}
    role_invoice:  {strategy: label, label: "Invoice address",  want: [CheckBox]}
    role_delivery: {strategy: label, label: "Delivery address", want: [CheckBox]}
    add_address:   {strategy: anchored_icon,
                    anchor: {strategy: label, label: "Addresses"},
                    pick: last_by_top}       # the green + — used ONLY for §2.8 branch B
  misc:
    alias:       {strategy: label, label: "Alias name",   want: [Edit]}
    discount:    {strategy: label, label: "Discount",     want: [Edit], expect: "0%"}
    net_gross:   {strategy: label, label: "Net or Gross", want: [ComboBox], expect: "Net"}
  payment:
    method:      {strategy: label, label: "Payment", want: [ComboBox]}

# --- brief §2.10.2-2.10.6 ----------------------------------------------
payment_editor:
  list_add:      {strategy: list_header_button, list: navigator.terms_of_payment,
                  pick: only, verify_opens_editor: true}    # the green + at upper-right
  name:          {strategy: label, label: "Name",        want: [Edit]}
  description:   {strategy: label, label: "Description", want: [Edit]}
  account:       {strategy: label, label: "Account",     want: [Edit], expect: ""}
  code:          {strategy: label, label: "payment code", aliases: ["Code"], want: [ComboBox]}
  cash_discount: {strategy: label, label: "Cash discount", want: [Edit], expect: "0"}
  discount_days: {strategy: label, label: "Discount Days", want: [Edit], expect: "0"}
  net_days:      {strategy: label, label: "Net Days",      want: [Edit], expect: "0"}
  text_unpaid:   {strategy: label, label: "Text 'unpaid'",  want: [Edit], expect: ""}
  text_deposit:  {strategy: label, label: "Text 'deposit'", want: [Edit], expect: ""}
  text_paid:     {strategy: label, label: "Text 'paid'",    want: [Edit], expect: ""}
  set_standard:  {strategy: name, name: "Set as standard", never_click: true}   # §2.10.5

# --- brief §3.4-3.6 -----------------------------------------------------
vat_editor:
  list_add:     {strategy: list_header_button, list: navigator.vats, pick: only}
  name:         {strategy: label, label: "Name",        want: [Edit]}
  description:  {strategy: label, label: "Description", want: [Edit]}
  value:        {strategy: label, label: "Value",       want: [Edit]}
  code:         {strategy: label, label: "VAT code (E-Invoice)", want: [ComboBox],
                 expect: "S (Standard rate)"}
  standard_vat: {strategy: label, label: "Standard VAT", never_write: true}      # §3.6

# --- brief §3.8-3.10 ----------------------------------------------------
product_editor:
  item_number:  {strategy: label, label: "Item Number", want: [Edit]}
  name:         {strategy: label, label: "Name",        want: [Edit]}
  description:  {strategy: label, label: "Description", want: [Edit]}
  price_gross:  {strategy: label, label: "Price",       qualifier: "gross", want: [Edit]}
  cost_price:   {strategy: label, label: "cost price",  want: [Edit], expect: "0.00"}
  vat:          {strategy: label, label: "VAT",         want: [ComboBox]}
  stock:        {strategy: label, label: "Stock",       want: [Edit], expect: "0.00"}
  untouched:    ["Category", "GTIN", "supplier code", "allowance",
                 "Product Picture", "user defined field 1"]                      # §3.10

# --- brief §5.1-5.3 -----------------------------------------------------
invoice_editor:
  invoice_no:    {strategy: label, label: "Invoice No.",  readonly_intent: true}
  invoice_date:  {strategy: label, label: "Invoice Date", readonly_intent: true}
  service_date:  {strategy: label, label: "Service date", readonly_intent: true}
  payment_method:{strategy: label, label: "Payment",      want: [ComboBox]}
  paid:          {strategy: label, label: "paid",         want: [CheckBox]}
  payment_date:  {strategy: label, label: "payment date", want: [Edit],
                  enabled_only_when: paid}
  value:         {strategy: label, label: "Value",        want: [Edit],
                  enabled_only_when: paid}

# --- brief §4.5 / §5.5 --------------------------------------------------
documents_view:
  search:  {strategy: label, label: "Search", want: [Edit]}
  grid:    {strategy: first_of, control_type: [DataGrid, Table, List]}
  columns: {strategy: table_header,
            headers: ["Document", "Date", "Cust.Ref.", "Name", "State", "Total"]}

dialogs:
  select_address: {title: "Select the address", search: {strategy: label, label: "Search"},
                   grid: {strategy: first_of, control_type: [DataGrid, Table, List]},
                   columns: ["Company", "First Name", "Name", "ZIP", "City"],
                   ok: {strategy: name, name: "OK"}, cancel: {strategy: name, name: "Cancel"}}
  select_product: {title: "Select a product",   search: {strategy: label, label: "Search"},
                   grid: {strategy: first_of, control_type: [DataGrid, Table, List]},
                   columns: ["Item Number", "Name", "Price"],
                   ok: {strategy: name, name: "OK"}, cancel: {strategy: name, name: "Cancel"}}
```

Three conventions in that file carry real weight:

- **`expect:`** is a post-condition, not an input. After the flow writes (or deliberately does not write) the field, the verifier asserts the read-back equals it. That is how "leave `Account` blank", "`Discount` 0%", "`Cash discount` 0" become machine-checked rather than hoped-for.
- **`readonly_intent: true` / `never_write: true` / `never_click: true`** encode the brief's *prohibitions* — proposed `No.`, proposed `Customer ID`, proposed Invoice No./Date/Service date, `Standard VAT`, `Set as standard`. `actions.py` refuses to write to a control carrying these flags, so a coding mistake raises instead of silently violating §1.4 / §2.6 / §2.10.5 / §3.6 / §5.1.
- **`scope:`** disambiguates labels that legitimately appear twice in one editor. `VAT` is both the header mode combo (`With VAT`) and a totals readout; `Discount` is both a line column and the order-level field. Without a scope these are exactly the near-tie that S1's `AMBIGUITY_MARGIN_PX` refuses — which is correct behaviour but stops the run, so the scope is declared once here instead.

Every `verify_opens` is enforced: after the click we `wait_modal` on that title, and if a different window appears we close it and raise. That is the guardrail against S4 clicking the green `+` by mistake — the exact failure the brief warns about twice.

---

## 10. CLI, artifacts, and the run report

```bash
fic run  data/input/order_001.png            # full flow
fic run  ... --dry-run                       # extract + reconcile + resolve every locator,
                                             # take screenshots, click nothing that mutates
fic run  ... --extractor ocr                 # skip the LLM
fic run  ... --stop-after resolve_debtor     # partial runs for the timebox
fic run  ... --resume runs/2026-08-28T10-14  # continue from state.json
fic extract data/input/order_001.png -o out.json
fic probe                                    # dump the UIA tree of the focused editor
                                             # (indispensable while developing locators)
```

`fic probe` deserves emphasis: it prints the live accessibility tree with control types, names, automation ids and rects, and marks which nodes each `selectors.yaml` entry currently resolves to. Building the locator map without it means guessing.

**Per-run artifacts** in `runs/<iso-timestamp>/`:

```
extraction.json      # the SourceOrder that was extracted
state.json           # RunState after the last completed transition
trace.jsonl          # one line per action
screenshots/         # 001_order_opened.png, 014_debtor_selected.png, ...
report.md            # rendered summary; the deliverable's "annotated screenshots"
```

A trace line:

```json
{"ts":"2026-08-28T10:14:22.118Z","step":"s2_debtor","action":"set_text",
 "target":"order_editor.cust_ref","strategy":"S1_label_anchor","anchor_rect":[412,208,468,224],
 "resolved_rect":[476,205,690,227],"wrote":"WEB-2026-0714-A17",
 "readback":"WEB-2026-0714-A17","ok":true,"elapsed_ms":214,"screenshot":"003_custref.png"}
```

Because `strategy` is recorded per action, `report.md` can state something concrete like "47 of 52 controls resolved via UIA (S0/S1), 5 needed vision (S4)" — which is exactly the evidence the design-doc question about grounding wants.

Exit codes: `0` done & verified · `2` manual review required (reason + screenshot + candidate dump) · `3` verification failure · `4` control not found · `5` extraction/reconciliation failure.

---

## 11. Error handling, retries, idempotency

**Retry only transient things.** Locator resolution and `wait_until` retry within their timeout. Business decisions never retry: an ambiguous debtor is ambiguous the second time too.

**`ManualReviewRequired` carries evidence** — reason code, the candidate rows or controls that caused it, a full-window screenshot, and the run state. It is a normal outcome, printed as a report, not a stack trace.

**Idempotency is the honest weak spot and should be named as such in the README.** Fakturama proposes document numbers; a second run creates a second Order. Mitigations, in order of cost:

1. Pre-flight: open `Data > Documents`, search the external reference; if an Order with this Cust.Ref. already exists ⇒ stop with `ALREADY_PROCESSED` unless `--force`.
2. `--dry-run` for development, so the workspace stays clean.
3. For e2e tests: keep a pristine copy of the Fakturama workspace directory and restore it before each run (`shutil.rmtree` + `copytree`) with the app closed. This is the only reliable reset and it belongs in a fixture, not in the flow.

**Read-only DB access as a *secondary* oracle.** Fakturama 2.x stores data in an embedded database under the workspace. Reading it after a run is a cheap, strong assertion for the e2e test. It must **never** substitute for the UI verification the brief requires (§4.5, §5.5) and must never be used for a write or as the existence check (R2). Gate it behind `--verify-db` and label it clearly as test-only.

---

## 12. Testing

| Layer | What | How it runs |
|---|---|---|
| unit | `normalize` (dates/decimals/percent/country), `reconcile` (all 7 rules incl. deliberate mismatches), `expected_line_net`, `product_master_gross` | pure, fast, CI-able on Linux |
| unit | extractor against `data/golden/order_001.json` | mocked API response for CI; a `--live` marker for the real call |
| contract | locator engine against **recorded UIA snapshots** — `fic probe --dump` serializes the tree to JSON; tests replay it and assert each `selectors.yaml` entry resolves to the expected node, including the ambiguity refusal | fast, no Fakturama needed, runs on Linux |
| e2e | full flow against a freshly restored workspace | Windows only, manual/nightly |

The contract layer is the one that pays off. It lets locator logic be developed and regression-tested without a running app, and a recorded snapshot doubles as documentation of what Fakturama's tree actually looks like.

---

## 13. Timebox plan

Two separate clocks, and they are graded separately. The brief says scope exceeds the clock deliberately, so both orderings below front-load the parts that prove the approach.

### 13.0 Part 1 — the design doc (90 minutes, ≤4 pages, no code)

**`docs/design.md` is not this file.** This spec is the working engineering document; `design.md` is a derived artifact under a hard page cap, and shipping 45 KB of Python where 4 pages of prose were requested reads as an inability to prioritise. The trim is itself part of the deliverable.

| Minutes | Work | Lands in design.md as |
|---|---|---|
| 0–15 | Read the brief, extract the 9 hard rules (§0) and the sample image | ½ page: problem statement + the Order-first flow diagram from §0 |
| 15–45 | Grounding strategy — the S0→S4 cascade, label-anchored resolution, the ambiguity refusal | 1½ pages: the cascade table (§5), one worked example of S1, why S4 always verifies via the dialog it opens |
| 45–65 | Extraction strategy — vision-primary / OCR-fallback, strict schema, the reconciliation gate | 1 page: §4 condensed, the 7 reconcile rules as a list, the "never silently fix arithmetic" stance |
| 65–80 | Tradeoffs + risks | ¾ page: the §14 table cut to its five strongest rows, plus the item-grid canvas risk (§7) named as *the* risk |
| 80–90 | Page-count check, trim, export | — |

**What is cut from design.md and why:** all code blocks (the brief says no code), the repo layout (§2), the full `selectors.yaml` (§9), the CLI surface (§10), and the testing matrix (§12). Each survives as a single sentence. Anything a reader would need to *build* it stays here in SPEC.md; only what a reader needs to *evaluate the approach* goes into design.md.

### 13.1 Part 2 — the implementation (5 hours, ordered by demo value)

| Slot | Work | Fallback if it overruns |
|---|---|---|
| 0:00–0:30 | Repo skeleton, config, models, CLI shell | — |
| 0:30–1:15 | Extraction: vision call + reconcile + golden test. **Fully working, end to end.** | none needed; this is low-risk |
| 1:15–2:15 | `fic probe` + the locator engine (S0/S1) + waits + verified actions | S1 only; defer S2/S3 |
| 2:15–3:00 | Step 1 (open Order, header) and Step 2 select-path for an existing Debtor | — |
| 3:00–3:45 | Step 2 creation branch + payment-method sub-branch | ship select-path only; document the gap |
| 3:45–4:30 | Step 3: product select-path + line completion (§7 probe decides the grid path) | if the grid is canvas-only and the keyboard protocol resists, spend the time on read-back-by-OCR rather than on a second grid strategy |
| 4:30–4:50 | Step 4 save+verify, Step 5 follow-up Invoice + paid state | if Step 5 is unreachable, ship Step 4 verified and write up Step 5 precisely — a correct written plan beats a half-clicked invoice |
| 4:50–5:00 | **Deliverables gate (D2–D6):** commit history tidy, README with setup + "what I skipped" + the 3-more-hours answer, curate `docs/figures/` from `runs/<id>/screenshots/` | never skipped — this slot is protected. An unshipped README costs more marks than one missing flow branch |

**What I'd cut first, in order:** VAT creation branch (Fakturama ships `VAT 19%` by default, so the select-path almost certainly hits), the OCR extractor, `--resume`, the DB oracle.

---

## 14. Tradeoffs, stated plainly

| Decision | Why | What it costs |
|---|---|---|
| Vision LLM as primary extractor, OCR as fallback | The source is a clean synthetic PNG with a table; an LLM with a strict schema handles layout variation that a template parser cannot | API dependency, per-run cost, non-determinism — bounded by the strict schema and the reconciliation gate |
| UIA-first, vision-last grounding | UIA is fast, deterministic, and resolution-independent; vision is slow and probabilistic but is the only thing that works on canvas-drawn widgets | Two code paths to maintain; mitigated by making S4 always verify via the dialog it opens |
| Label-anchored spatial resolution rather than tab-index or ordinals | Survives layout reflow, window resizing, and locale changes in a way ordinals do not | Needs an ambiguity policy; solved by the margin check + refusal |
| Refuse on ambiguity instead of picking the best candidate | In an accounting system, a wrong Debtor is worse than no Debtor | Lower autonomy; more manual-review exits — which the brief explicitly asks for |
| Read-back verification after every write | Catches SWT's silent commit-on-focus-out quirks and the grid's blind writes | Roughly doubles per-field interaction time; worth it |
| Order-first, master data created only on miss | Directly mandated (R2/R3), and it avoids duplicate master records | More UI navigation than a "create everything up front" approach |
| No `sleep()`, condition-based waits only | Deterministic on slow VMs, fast on quick ones | More code than `sleep(2)`; pays for itself immediately |

**Known weaknesses I would state in the README rather than hide:** re-running duplicates documents; the item grid strategy is chosen at runtime and the canvas path is the least-tested code; German/English UI locale is handled by an alias table that is only populated for the labels this flow touches; multi-page or photographed (skewed, shadowed) source images are not handled — no deskew, no perspective correction.

---

## 15. Written question — "if you had 3 more hours"

1. **Harden the grid** (~60 min). The canvas path is the single biggest correctness risk. Build a proper column-order learner that runs once, caches to the run state, and validates itself by writing a known value into each column and reading the totals back.
2. **Idempotency + reset fixture** (~45 min). Pre-flight duplicate detection on Cust.Ref., and a `pytest` fixture that restores a pristine workspace so e2e can run repeatedly and unattended.
3. **Locator regression corpus** (~45 min). Record UIA snapshots for every dialog and editor the flow touches and pin them as contract tests, so a Fakturama version bump produces a failing test rather than a mid-run crash.
4. **Multi-image batch + report** (~30 min). Run a directory of orders, produce one HTML summary with per-order status, the strategy mix, and links to the annotated screenshots — the artifact that makes the system's reliability legible.

---

## Appendix A — figure reference

Extracted from the brief into `docs/`, in document order:

| File | Shows |
|---|---|
| `sample_order.png` | the source order image (the golden fixture in §3) |
| `image1.png` | Figure 1 — `Select the address` dialog over the open Order |
| `image9.png` | Figure 2 — Main address with the Invoice address role |
| `image5.png` | Figure 4 — payment method with the `Credit transfer` code |
| `image6.png` | Figure 5 — `Select a product` dialog |
| `image7.png` | Figure 6 — `VAT 19%` with `S (Standard rate)` |
| `image4.png` | Figure 7 — product creation, gross `297.50`, VAT selected |
| `image3.png` | Figure 8 — saved Order with `Create a follow-up document` |
| `image2.png` | Figure 9 — linked Invoice populated from the Order |
| `image10.png` | Figure 10 — final verification in `Data > Documents` |

---

## Appendix B — brief-to-spec traceability

Every numbered instruction in the brief, mapped to where this spec covers it. This table is the checklist used to sign off the implementation; a step with no verified read-back in the run trace is not done, regardless of what the code claims.

| Brief | Instruction | Covered by |
|---|---|---|
| 1.1 | OCR/LLM extraction of the supplied image | §4.1 vision · §4.2 OCR fallback · §4.5 preflight |
| 1.2 | Extract order, debtor, address, payment and every item field | §3 `SourceOrder` · §4.3 normalize |
| 1.3 | Click `Order` in the top toolbar, wait for New Order editor | §8 Step 1 · §9 `toolbar.order` |
| 1.4 | Leave the proposed `No.` unchanged | §8 Step 1 (recorded as `state.order_no`) · §9 `readonly_intent` |
| 1.5 | `Date` ← extracted Order Date | §8 Step 1 · §9 `order_editor.date` |
| 1.6 | `Cust.Ref.` ← External Reference | §8 Step 1 · §9 `order_editor.cust_ref` |
| 1.7 | Price mode `Net`, VAT `With VAT` | §8 Step 1 · §9 `net_gross`, `vat_mode` |
| 1.8 | Keep the New Order tab open throughout | R4 · §5 scoping · asserted at every step entry |
| 2.1 | Upper existing-contact icon, **not** the green + | §5 S4 · §9 `select_contact_icon` (`verify_opens`) |
| 2.2 | Search by company, wait for list to stabilize | §8 Step 2 · §6 `wait_stable` |
| 2.3 | Exact on Company/First Name/Name/ZIP/City; 1 → OK, conflict → review, 0 → create | §8 Step 2 outcome table · R5 |
| 2.4 | Confirm populated Invoice + Delivery address | §8 Step 2 · §9 `*_address_block` |
| 2.5 | `New Contact` in the left New panel, Order stays open | §8 creation branch · §9 `navigator.new_contact` |
| 2.6 | Proposed Customer ID unchanged; Company/First/Last; Salutation `---` | §8 creation branch · §9 `debtor_editor` |
| 2.7 | Main address fields; optional additional name / Address specification / district | §8 creation branch · §9 `debtor_editor.address` |
| 2.8 | `Invoice address` role; also `Delivery address` **iff** identical | §3 trap note · §8 role assignment (branch-aware) |
| 2.9 | Miscellaneous: Alias name, Discount `0%`, Net or Gross `Net` | §8 creation branch · §9 `debtor_editor.misc` |
| 2.10 | Payment tab: select exact method, else create | §8 creation branch · §9 `debtor_editor.payment` |
| 2.10.1 | `Data > terms of payment`, search exact | §8 sub-branch · §9 `navigator.terms_of_payment` |
| 2.10.2 | 1 exact → reuse · multiple/conflict → review · none → green + | §8 sub-branch · §9 `payment_editor.list_add` |
| 2.10.3 | Name + Description = method; Account blank | §8 sub-branch table · §9 `expect: ""` |
| 2.10.4 | Payment-code mapping (Credit transfer / Credit card / SEPA) | §8 sub-branch table · closed map in `config/app.yaml` |
| 2.10.5 | Cash discount/Discount Days/Net Days `0`; texts blank; **not** `Set as standard` | §8 sub-branch table · §9 `never_click` |
| 2.10.6 | Save once, return, select the new method | §6 `save()` (R6) · §8 sub-branch |
| 2.11 | Save the Debtor once | §6 `save()` (R6) |
| 2.12 | Return to the open Order, reselect the new Debtor | §8 creation branch |
| 2.13 | Successful selection **is** the save confirmation | R2 · §8 creation branch (no DB peek) |
| 3.1 | Run the branch for every item, in source order | §8 Step 3 loop |
| 3.2 | Upper Product-selection icon, **not** the green + | §9 `select_product_icon` (`verify_opens`) |
| 3.3 | Search exact SKU; 1 → OK · conflict → review · 0 → create | §8 Step 3 |
| 3.4 | `Data > VATs` **before** New product | §8 Step 3 (VAT first) · §9 `navigator.vats` |
| 3.5 | Reuse only if Name + Value + `S (Standard rate)` all match | §8 Step 3 · §9 `vat_editor.code` |
| 3.6 | Create VAT; leave `Standard VAT` unchanged; save once | §8 Step 3 · §9 `never_write` |
| 3.7 | `New product` only after the VAT exists | §8 Step 3 ordering |
| 3.8 | Item Number ← SKU; Name **and** Description ← description | §8 product table |
| 3.9 | Price (gross) = net × (1+VAT/100), 2dp, **no** line discount | §3 `product_master_gross()` · §8 product table |
| 3.10 | cost price `0.00`, exact VAT, Stock `0.00`, six fields untouched | §8 product table · §9 `product_editor.untouched` |
| 3.11 | Save once | §6 `save()` (R6) |
| 3.12 | Reselect the new Product; not found → review | §8 Step 3 |
| 3.13–3.16 | Qty · U.Price · VAT · Discount · assert line Price | §7 grid strategy · §8 complete-the-line |
| 3.17 | Repeat for every remaining item | §8 Step 3 loop |
| 4.1 | Re-confirm addresses and every line against the image | §8 Step 4 |
| 4.2 | Overall Discount `0%`, Shipping free / `0.00` | §8 Step 4 · §9 `order_editor.discount`, `shipping` |
| 4.3 | Total Net / VAT / Total match the source totals | §7 totals cross-check · §8 Step 4 |
| 4.4 | Save once | §6 `save()` (R6) |
| 4.5 | `Data > Documents`: one Order row, number/date/Cust.Ref./open/Total | §8 Step 4 · §9 `documents_view` |
| 4.6 | Invoice from `Create a follow-up document`, **not** the toolbar | R7 · §9 `followup_invoice` |
| 4.7 | Wait for the linked New Invoice editor | §8 Step 4 |
| 5.1 | Leave Invoice No./Date/Service date; verify all copied fields | §8 Step 5 · §9 `readonly_intent` |
| 5.2 | Payment method must match; unavailable → review (no creation here) | §8 Step 5 |
| 5.3 | PAID → `paid` + payment date + Value = full Invoice Total; else invent nothing | §8 Step 5 |
| 5.4 | Save once | §6 `save()` (R6) |
| 5.5 | `Data > Documents`: Invoice state/Total; Order still `open`, same Cust.Ref./Total | §8 Step 5 |
| 5.6 | Reopen the Invoice only if needed to confirm persistence | §8 Step 5 (optional) |
| 5.7 | Stop — no Delivery, Correction, or Dunning | R9 · §8 Step 5 terminates |

**Deliverables (unnumbered in the brief) → §0.1 D1–D6.**

Non-obvious readings this spec commits to, so they can be argued with rather than discovered late:

- **§2.8 has an unstated else-branch.** The brief only says what to do when billing and delivery are *identical*. The sample's addresses differ (different recipient, street and ZIP), so the flow adds a second address carrying the `Delivery address` role while Main keeps `Invoice address` only. See §3 and §8 Step 2.
- **§3.9's "Do not apply the transaction-line discount"** means the Product master price for `CHR-ERG-01` is `250.00 × 1.19 = 297.50`, not the discounted `225.00 × 1.19`.
- **§5.3's "full Invoice Total"** is the gross `678.30`, not the net.
- **§4.3 "match the source totals"** is checked against the *printed* totals from the image, which §4.4 has already proved internally consistent — so a mismatch here indicts the UI interaction, not the extraction.
- **The brief numbers its figures 1, 2, 4–10; there is no Figure 3.** Appendix A follows the brief's own numbering rather than renumbering it.
