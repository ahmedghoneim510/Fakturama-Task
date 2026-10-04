# Interview Prep — Fakturama Image-to-Cash

A study guide for explaining this project in an interview: what it does, how one
run moves through the code, the Python concepts it uses, and the questions you
are likely to get (with answers).

---

## 1. The 30-second pitch (say this first)

> "It's a Python CLI that turns **a picture of a purchase order** into **real,
> saved accounting documents** inside Fakturama, a desktop invoicing app.
> An AI vision model (Gemini or Claude) reads the image into structured JSON.
> I validate that JSON in two tiers: types first, then the arithmetic.
> Then I drive Fakturama's GUI through **Windows UI Automation**. The tool finds
> or creates the customer, finds or creates each product and VAT rate, fills the
> order lines, checks the totals and saves the Order. Then it creates a linked
> Invoice and marks it paid.
> The core rule: **act → wait → read back → compare → only then continue.**
> When something is ambiguous, it stops and asks a human instead of guessing,
> because every value is money."

Key words to use: **pipeline**, **two-tier validation**, **read-back
verification**, **halt on ambiguity**, **idempotency**, **no hardcoded
coordinates**, **provider-agnostic extraction**.

---

## 2. Tech stack

| Area | Library | Why it's here |
|---|---|---|
| CLI | `typer` + `rich` | Commands (`fic run/extract/probe`) and colored output |
| Data validation | `pydantic` v2 | `SourceOrder` model = tier-1 validation + JSON schema for the LLM |
| Money | `decimal.Decimal` | Exact math, never `float` |
| AI extraction | `google-genai`, `anthropic` | Vision models read the image to JSON |
| GUI automation | `pywinauto` (UIA backend), `uiautomation`, `comtypes` | Drives Fakturama's Windows UI |
| Config | `pyyaml`, `python-dotenv` | `config/*.yaml` + `.env` secrets |
| Tests | `pytest`, `pytest-mock` | 60 unit tests, no GUI and no network |
| Tooling | `uv`, `ruff`, `hatchling` | Env/deps, lint, build |

Runs on **Windows only** for the GUI part (UIA is a Win32 API).
`fic extract` and all the tests also run on Linux/macOS.

---

## 3. Project map

```
src/fic/
├── cli.py          # Entry point. Typer commands: run / extract / probe. Maps errors → exit codes
├── __main__.py     # lets you run `python -m fic`
├── models.py       # Pydantic domain model: SourceOrder, SourceItem, SourceDebtor... (TIER 1)
├── extraction.py   # Image → LLM → SourceOrder; reconcile() (TIER 2); retries; cross-check
├── flow.py         # THE ORCHESTRATOR: run_flow() = Phases 1..5
├── errors.py       # Exception hierarchy + exit codes
├── config.py       # Loads config/app.yaml + selectors.yaml (cached)
├── report.py       # Writes runs/<timestamp>/ extraction.json, state.json, trace.jsonl, report.md
└── uia/            # Everything that touches the GUI
    ├── session.py          # Launch or attach to Fakturama, find the real main window
    ├── locator.py          # "Find the control": by name, by label position, grids, rows
    ├── actions.py          # "Do something safely": set_text, select_combo, click, save (all verified)
    ├── waits.py            # wait_until / wait_stable: polling instead of fixed sleeps
    ├── grid.py             # Editing cells in the order Items grid
    └── contact_resolver.py # Reads Fakturama's HSQLDB .script file (advisory lookups only)

config/app.yaml        # timeouts, date format, tolerance, payment-code map
config/selectors.yaml  # semantic descriptions of UI controls (no coordinates!)
data/golden/*.json     # "known correct" extractions, used by tests and --from-json
tests/                 # pydantic + reconcile + resolver + mocked-LLM tests
```

**Layers (top to bottom):** `cli` → `extraction` / `flow` → `uia.locator` /
`uia.actions` / `uia.waits` → `pywinauto` → Windows UIA → Fakturama.

---

## 4. The lifecycle of one run

```
 fic run order_001.png
        │
        ▼
 ┌──────────────────────────┐
 │ 0. SETUP                  │  load .env, create runs/<timestamp>/
 └──────────────────────────┘
        │
        ▼
 ┌──────────────────────────┐   Gemini/Claude reads image → JSON
 │ 1. EXTRACT                │   → SourceOrder.model_validate()   (TIER 1: types, rules)
 │    extraction.py          │   → reconcile()                    (TIER 2: arithmetic)
 │                           │   → completeness check, re-read if needed
 └──────────────────────────┘      ✗ ExtractionError (5) / ManualReviewRequired (2)
        │  (--dry-run stops here, exit 0)
        ▼
 ┌──────────────────────────┐
 │ 2. IDEMPOTENCY CHECK      │   same Cust.Ref already saved? → AlreadyProcessedError (6)
 └──────────────────────────┘   (reads DB file, BEFORE opening the GUI)
        │
        ▼
 ┌──────────────────────────┐
 │ 3. SESSION                │   attach to Fakturama, or launch it and wait for the toolbar
 └──────────────────────────┘
        │
        ▼   flow.run_flow()
 ┌──────────────────────────┐
 │ Phase 1  open_order       │   New Order; read proposed No.; set Date, Cust.Ref, Net, VAT mode
 │ Phase 2  debtor           │   try_select_debtor → else create_debtor (+ payment method)
 │ Phase 3  per item line    │   find SKU → else ensure_vat + create_product → complete_line
 │ Phase 4  verify + save    │   read Total Net / VAT / Total, compare to source, Save once
 │ Phase 5  invoice          │   "Create a duplicate → Invoice", verify, set paid + date, Save
 └──────────────────────────┘
        │
        ▼
 ┌──────────────────────────┐
 │ 4. REPORT                 │   state.json + report.md; exit code 0
 └──────────────────────────┘
```

On **any** error, `cli.py` catches it, writes `report.md` with the log collected
so far plus the error "evidence", and exits with that error's `exit_code`.

### Exit codes (from `errors.py`)

| Code | Exception | Meaning |
|---|---|---|
| 0 | — | done, verified |
| 1 | any other `Exception` | unexpected bug |
| 2 | `ManualReviewRequired` (+ `AmbiguousControl`, `OptionUnavailable`) | a business rule says a human must decide |
| 3 | `VerificationFailed` | wrote something, but the read-back didn't match |
| 4 | `ControlNotFound` | couldn't find a UI control (or a wait timed out) |
| 5 | `ExtractionError` | the LLM output was malformed; nothing in the GUI was touched |
| 6 | `AlreadyProcessedError` | this order was already saved (use `--force` to override) |

---

## 5. Worked example: `order_001`

Input (`data/golden/order_001.json`, i.e. what the model extracts from `order_001.png`):

```
Order WEB-2026-0714-A17, 2026-07-14, Northstar Office GmbH (Marta Klein), Berlin
Payment: Bank Transfer, PAID on 2026-07-18
Line 1: CHR-ERG-01  Ergonomic Desk Chair   qty 2 × 250.00, discount 10 %, VAT 19 %  → 450.00
Line 2: MAT-DESK-02 Anti-Fatigue Desk Mat  qty 3 ×  40.00, discount  0 %, VAT 19 %  → 120.00
Net 570.00   VAT 108.30   Gross 678.30
```

**Step A — Tier 1 (pydantic).** Is every field the right type? Is `quantity > 0`?
Is the SKU non-blank? `PAID` needs a `payment_date`, and here it has one. ✓

**Step B — Tier 2 (`reconcile()`).** Recompute everything with `Decimal` and
compare within a tolerance of `0.02`:

```
line 1: 2 × 250.00 × (1 − 10/100) = 450.00   printed 450.00  ✓
line 2: 3 ×  40.00 × (1 −  0/100) = 120.00   printed 120.00  ✓
net   : 450 + 120 = 570.00                    printed 570.00  ✓
VAT   : 450×0.19 + 120×0.19 = 85.50 + 22.80 = 108.30          ✓
gross : 570.00 + 108.30 = 678.30                               ✓
```

If any check failed, you'd get `ManualReviewRequired` (exit 2), and the tool
would **never auto-correct** the number.

**Step C — Idempotency.** Search saved documents for Cust.Ref `WEB-2026-0714-A17`.
If it's found, stop with exit 6.

**Phase 1.** Click "Create: New Order". Read the proposed number (e.g. `PO000003`)
and **don't change it**. Pre-check that this number isn't already used (a
counter bug after force-kills). Set Date (`07/14/2026`, since this install uses
the US date format), Cust.Ref, price mode = Net.

**Phase 2 — Debtor.** Open "Select the address" and search. A row counts as a
match only if **all five fields match exactly**: Company, First, Last, ZIP, City.
- 1 match → click the row, then click OK. (Clicking the row matters: OK with no selected row silently does nothing.)
- 2+ matches → halt, a human decides.
- 0 matches, but the DB says this company exists → **halt** (don't create a duplicate customer).
- 0 matches → `create_debtor`: fill the Contact editor. The delivery address differs here ("Northstar Office Warehouse"), so uncheck "Delivery equals Invoice" and fill the mirrored right-hand column. Ensure the payment method exists, Save, then **re-select it from the Order** to prove the save worked.

**Phase 3 — Products** (loop over `source.items`). Search by exact SKU.
- Found → select it.
- Not found → `ensure_vat` (reuse "VAT 19%" or create it) → `create_product`. The master gross price = `250.00 × 1.19 = 297.50` (the "canary" test). Then search again and select.
- `complete_line`: write Qty = 2, U.Price, VAT, Discount = 10 into the grid, and read each one back.

**Phase 4.** Read Total Net / VAT / Total from the screen:
`570.00 / 108.30 / 678.30`. If they match within 0.02, click **Save once**.
Save counts as confirmed when the tab title loses its `*` (`*New Order` → `PO000003`).
If an unexpected dialog appears instead, halt.

**Phase 5.** In the saved Order, click "Create a duplicate → Invoice". This keeps
the Order and the Invoice linked in the DB (same transaction id). Re-verify the
inherited totals, set payment method = Bank Transfer, tick paid, set the payment
date to 2026-07-18 and the value to 678.30, then Save → `INV000001`.

**Output folder:** `runs/2026-…/extraction.json`, `state.json`, `trace.jsonl`, `report.md`.

---

## 6. Core design ideas (the "why")

1. **Act → wait → read back → compare → advance.** For example, `actions.set_text`
   types the value, presses Tab and re-reads the field. If the text differs, it
   raises `VerificationFailed`.
2. **Read back what *persists*, not what you typed.** A combo box can *display*
   "19%" while nothing was actually selected. There was a real bug here: a
   product got saved with a 0 % VAT rate under a correct-looking name.
3. **No coordinates.** Controls are found by name, or **relative to their visible
   label** (`locator.resolve_by_label`: the nearest control to the right of the
   "Cust.Ref." text). If two candidates are within 12 px of each other, it raises
   `AmbiguousControl` instead of picking one.
4. **No fixed sleeps for UI state.** `wait_until(predicate)` polls every 0.15 s
   until the condition is true or a timeout hits. `wait_stable(fn)` waits until a
   value (like a row count) stops changing.
5. **Halt on ambiguity.** `ManualReviewRequired` is a normal, expected outcome and
   is never retried: "an ambiguous debtor is ambiguous the second time too."
6. **The DB is advisory; the UI is the authority.** `contact_resolver.py` reads
   Fakturama's HSQLDB `.script` file for three things: choosing a good search
   term, the duplicate-number check, and the "customer already exists" safety
   check. The actual selection always goes through the UI.
7. **Provider-agnostic extraction.** Gemini (default) and Claude both return the
   same `SourceOrder`. You switch with `--provider` or `FIC_PROVIDER`.
   `--cross-check` runs both and halts if the money fields disagree.
8. **Retry only what is transient.** A Gemini 503/429/timeout gets retried with
   exponential backoff (2, 4, 8, 16 s). A bad key or a retired model fails
   immediately with a helpful hint.

---

## 7. Python topics used here (brief, with real code)

### 7.1 Type hints and `from __future__ import annotations`
Every module starts with `from __future__ import annotations`. This allows
modern syntax like `str | None` on Python 3.10 and lets a class refer to itself
(`-> SourcePayment`) inside its own body.
```python
first_name: str | None = None
paid_status: Literal["PAID", "UNPAID"]      # only these two strings are allowed
```

### 7.2 Pydantic models and validators (`models.py`)
`BaseModel` parses and validates dicts or JSON into typed objects.
```python
class SourceItem(BaseModel):
    quantity: Decimal

    @field_validator("quantity")          # checks ONE field
    @classmethod
    def qty_positive(cls, v):
        if v <= 0:
            raise ValueError("quantity must be > 0")
        return v

class SourcePayment(BaseModel):
    @model_validator(mode="after")        # checks the WHOLE object (cross-field)
    def paid_requires_date(self):
        if self.paid_status == "PAID" and self.payment_date is None:
            raise ValueError("PAID but no payment_date")
        return self
```
Other pydantic APIs used: `SourceOrder.model_validate(dict)`,
`model_validate_json(text)`, `model_dump_json(indent=2)`,
`model_json_schema()` (this schema is sent to the LLM so it knows the exact
shape), and `Field(min_length=1)` (at least one item).

### 7.3 `Decimal` for money, never `float`
```python
0.1 + 0.2 == 0.3                            # False with float!
Decimal("0.1") + Decimal("0.2") == Decimal("0.3")   # True

def q2(x):   # round half-up to 2 decimals: the one rounding rule used everywhere
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```
Gotcha found in this project: `str(Decimal("20").normalize())` gives `'2E+1'`,
and Fakturama read that as 0 %. The fix was `format(d.normalize(), "f")` → `'20'`.

### 7.4 Dataclasses (`flow.RunState`, `contact_resolver.ContactRecord`)
Lightweight classes for plain data, where you don't need validation.
```python
@dataclass
class RunState:
    order_saved: bool = False
    log: list[str] = field(default_factory=list)   # never use a mutable default like `= []`

@dataclass(frozen=True)       # immutable and hashable
class ContactRecord: ...
```
`dataclasses.asdict(state)` turns it into a dict for `state.json`.

### 7.5 Custom exception hierarchy (`errors.py`)
```python
class AutomationError(Exception):
    exit_code = 1                                  # class attribute, overridden in subclasses
    def __init__(self, message, **evidence):       # **kwargs collects any extra details
        super().__init__(message)
        self.evidence = evidence

class ManualReviewRequired(AutomationError): exit_code = 2
class AmbiguousControl(ManualReviewRequired): ...  # inheritance: catching the parent catches it too
```
- `raise ExtractionError(...) from exc` does **exception chaining**: the original error is kept in `__cause__`.
- In the CLI, the order of `except` blocks matters: `typer.Exit` first (a normal exit), then `AutomationError`, then `Exception`.

### 7.6 Loops in this project (how `for` / `while` work here)
**a) Plain loop over the extracted items (the heart of Phase 3):**
```python
for item in source.items:
    resolve_product_line(session, editor, item, state)
```
**b) `enumerate` + `zip`: walk two lists in parallel with an index** (comparing two extractions):
```python
for i, (ai, bi) in enumerate(zip(a.items, b.items)):
    if ai.sku.casefold() != bi.sku.casefold():
        diffs.append(f"item {i} sku: ...")
```
**c) Retry loop with `try / except / else`** (`_gemini_generate`):
```python
for attempt in range(attempts):
    try:
        resp = client.models.generate_content(...)
    except Exception as exc:
        if not _is_transient(exc) or attempt == attempts - 1:
            raise                                  # give up
        time.sleep(2 ** (attempt + 1))              # 2, 4, 8, 16 s: exponential backoff
    else:                                           # runs only if NO exception happened
        return resp
```
**d) `while` loop with a deadline** (`waits.wait_until`, polling):
```python
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    result = predicate()
    if result:
        return result
    time.sleep(poll)
raise ControlNotFound("timed out ...")
```
`time.monotonic()` is used instead of `time.time()` because it never jumps back
when the system clock changes.

**e) `while` with a counter** (`extract_and_reconcile`: re-read the image while
important fields are missing, up to N times, and `break` early if nothing improves).

**f) `continue` to skip bad elements** (`locator.snapshot`): elements in a live UI
tree can vanish mid-loop, so each one is wrapped in `try/except: continue`.

**g) Comprehensions** instead of loops that build a list or dict:
```python
live = [r for r in all_records if not r.deleted]
mismatches = {k: {...} for k, v in checks.items() if v[0] is None or abs(v[0]-v[1]) > tol}
wanted = {_norm_label(label)} | {_norm_label(a) for a in aliases}   # set union
```
**h) Generator expression + `next(..., default)`**: find the first match or get `None`:
```python
tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
```
**i) `sum` with a `Decimal` start value**, so the result stays a `Decimal`:
```python
sum((i.line_net_total for i in order.items), Decimal(0))
```

### 7.7 Higher-order functions, closures and lambdas
`wait_until` takes a **function** as an argument and calls it repeatedly. The
caller often defines a small inner function (a **closure**) that captures local
variables:
```python
def _line_present():                 # closure: captures `editor` and `item`
    ...
wait_until(_line_present, timeout=10.0, description=f"line for {item.sku}")

wait_until(lambda: session.wait_for_any_tab("New Order", timeout=1), timeout=15.0)
max(anchors, key=lambda n: n.rect[1])     # lambda as a sort/selection key
```

### 7.8 Generics: `TypeVar` and `Callable`
```python
T = TypeVar("T")
def wait_until(predicate: Callable[[], T | None], ...) -> T:
```
This says: "whatever type the predicate returns, `wait_until` returns that same type."

### 7.9 Decorators
- `@dataclass`, `@property` (`delivery_same_as_billing` is read like an attribute), `@classmethod` (pydantic validators).
- `@lru_cache(maxsize=1)` in `config.py`: the YAML file is read once and cached.
- `@app.command()` (Typer): turns a function into a CLI command, and its parameters become arguments/options.
- `@pytest.mark.live`: tags a test so it's skipped by default.

### 7.10 `pathlib.Path`
```python
REPO_ROOT = Path(__file__).resolve().parents[2]     # go up 2 folders from this file
(run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
path.read_text(encoding="utf-8"); path.read_bytes(); path.suffix.lower()
```

### 7.11 Lazy imports
```python
def extract_with_claude(...):
    import anthropic      # imported only when actually needed
```
This keeps `fic --help` fast and lets code run without every SDK installed. The
same trick lets `pywinauto` (Windows-only) be imported only inside the GUI code.

### 7.12 Environment and config
`load_dotenv()` loads `.env` into `os.environ`; then
`os.environ.get("FIC_PROVIDER", "gemini")` reads with a default. `yaml.safe_load`
reads the config (`safe_` = it can't execute arbitrary objects).

### 7.13 Strings and text
- f-strings with `!r`: `f"{raw!r}"` shows the `repr` (with quotes), which is clearer in error messages.
- `casefold()` is a stronger `lower()` for Unicode comparisons. `" ".join(s.split())` collapses whitespace.
- `re` (regex): `re.sub(r"[^0-9.\-]", "", "20.59 $")` → `"20.59"`.
- `unicodedata` + a char map: normalizes Arabic letter variants for name matching.

### 7.14 Platform-specific code
```python
if sys.platform == "win32":
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
```
`ctypes` calls a Windows DLL directly so screen coordinates are correct on scaled displays.

### 7.15 Packaging and running
- `pyproject.toml` → `[project.scripts] fic = "fic.cli:app"` creates the `fic` command.
- `src/` layout, with `__main__.py` so `python -m fic` works.
- `uv sync` builds `.venv` from `uv.lock` (a reproducible install), and `uv run` runs inside that env.

### 7.16 Testing with pytest
- Tests are pure logic, with no GUI and no network, so all 60 run in about 1 second.
- `monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: fake_client)` swaps the real client for a mock.
- `MagicMock()` builds fake response objects. `call_args` checks the tool was *forced*: `tool_choice == {"type": "tool", "name": "record_order"}`.
- `pytest.raises(ExtractionError)` asserts that an error is raised.
- Markers: `addopts = -m "not e2e and not live"` skips network tests by default. Run them with `pytest -m live`.
- A golden fixture (`order_001.json`) is a known-correct answer that other tests compare against.
- `conftest.py` puts `src/` on `sys.path`.

### 7.17 LLM structured output ("tool forcing")
For Claude, the pydantic JSON schema becomes a tool's `input_schema`, and
`tool_choice` **forces** the model to call that tool. That means the output is
always structured JSON, never prose. For Gemini, the schema goes into the prompt
as text, and the code strips any Markdown code fences (the triple backticks) from
the reply before calling `json.loads`. Either way, pydantic validates the result.

---

## 8. Likely interview questions and answers

**Q: Why `Decimal` and not `float`?**
A float can't represent 0.1 exactly, so money totals drift. `Decimal` + one
rounding rule (`q2`, half-up) makes every computed value reproducible and comparable.

**Q: What's the difference between tier-1 and tier-2 validation?**
Tier 1 (pydantic) checks the *structure* of single fields: types, blank values,
qty > 0, PAID needs a date. A failure there is `ExtractionError`. Tier 2
(`reconcile`) checks *cross-field arithmetic*: line totals, VAT sum, and
net + VAT = gross. A failure there is `ManualReviewRequired`, because the data
itself is inconsistent.

**Q: Why the 0.02 tolerance?**
Three values are computed independently (the printed document, our
recomputation, Fakturama's own calculation). Each rounds, so exact equality
would fail on legitimate rounding.

**Q: What if the LLM reads a number wrong?**
Tier 2 catches most of it, since the totals won't add up. Also, a missing
critical field triggers a second read, and both reads must agree on money.
`--cross-check` compares two different providers. The rule: **never
auto-correct; halt.**

**Q: How do you find a button without coordinates or IDs?**
Fakturama exposes no `AutomationId`. So controls are found by accessible name
(`find_by_name`), or by label position (`resolve_by_label`: the nearest control
to the right of a text label, on the same row). If there are two close
candidates, it raises `AmbiguousControl`. Grid columns are read from the grid's
own header, never hardcoded.

**Q: How do you know Save actually worked?**
It doesn't trust the click. It waits until the specific tab loses its `*` dirty
marker. At the same time it watches for any new top-level window (e.g.
Fakturama's "Duplicate Contact" dialog). If a dialog appears, it halts. If
neither happens within 30 s, you get `VerificationFailed`. For a new contact,
it also re-selects the contact from the Order to prove it was saved.

**Q: What happens if I run the same image twice?**
The idempotency check finds the saved Cust.Ref in the DB file *before* the GUI
opens and exits with code 6. `--force` overrides it.

**Q: Why read the database file if the UI is the "authority"?**
Fakturama's search box does single-column substring matching, so searching
"Marta Klein" fails when first and last names are in separate columns. The DB
tells us *what to type* and powers safety checks (duplicate order number,
"customer already exists"). It never makes the selection itself.

**Q: Why not just use `time.sleep(3)`?**
A fixed sleep is either too short (flaky) or too long (slow). Polling a real
condition (`wait_until`) finishes as soon as the UI is ready and fails with a
clear message if it never is.

**Q: How is it testable without Windows or an API key?**
Business logic (models, reconcile, resolver parsing) is pure Python. The LLM
client is mocked. `--from-json` feeds a saved extraction into the flow, and
`--dry-run` stops before the GUI. The GUI part was verified live against real
Fakturama (see the README's verification table).

**Q: Which errors do you retry?**
Only transient ones: Gemini 503/429/timeouts (exponential backoff) and UI
slowness (inside `wait_until`). Never `ManualReviewRequired`, because an
ambiguous answer stays ambiguous.

**Q: Why an exception hierarchy with exit codes?**
Each outcome is distinct and machine-readable for a scheduler or operator.
`evidence` kwargs go into `report.md`, so the operator sees *why* a run stopped,
not only *that* it stopped.

---

## 9. Good "real bug" stories (interviewers love these)

Each one shows debugging against reality, not guessing:

1. **Save always "passed."** The dirty check read the main window's title, which never has a `*`. It was fixed to check the real `TabItem`. Then it *never* passed, because the Order tab stays dirty while you save a Contact. Fixed again by checking **only the target tab**.
2. **VAT 20 % became 0 %.** `Decimal("20").normalize()` → `"2E+1"`, which Fakturama parsed as 0. Fixed with `format(..., "f")`.
3. **OK did nothing.** Reading a row's text doesn't select it, so the code now clicks the row before OK.
4. **Wrong window at startup.** The splash screen matched the title test, so the code waited 180 s for a toolbar that would never appear there. Fix: pick the window that *has* the toolbar.
5. **Duplicate customer.** The extraction dropped the first/last names, so the 5-field match failed and a second customer was created. Fix: completeness check + re-read + halt if the DB already knows this company.
6. **Shipping ignored.** Totals were off by exactly the shipping amount. Fix: `reconcile` models shipping, and Phase 4 writes it into the order.
7. **`uv sync` "hung."** The `uv.exe` binary was truncated (538 KB instead of about 41 MB). Lesson: if a tool hangs on `--version`, it's broken, not slow.

---

## 10. Weak spots to admit (and how you'd fix them)

Saying these before you're asked shows maturity:

- **No automated end-to-end test suite.** The GUI flow was verified by hand against a live app. *Next step:* a `pytest -m e2e` suite with a resettable Fakturama workspace.
- **`config/selectors.yaml` is not read at runtime.** `config.selectors()` exists and `flow.py` has `SEL = selectors`, but nothing calls it. The labels are hardcoded in `flow.py` (e.g. `resolve_by_label(editor, "Cust.Ref.")`). The flags `never_click`, `never_write` and `readonly_intent` are not enforced anywhere, and one entry is stale (`"Create a follow-up document"`; the real panel is `"Create a duplicate"`). *Fix:* make `flow.py` resolve controls through the YAML, or call the file documentation and say so.
- **Duplicated logic:** `self_consistency_check()` re-implements what `money_diffs()` already does. It could just call `money_diffs`.
- **`if not from_json or True:`** in `cli.py` is always true. It's leftover code and should just be removed.
- **Idempotency is checked twice** (in the CLI and in `run_flow`). This is intentional (the check is pure and cheap), but it's worth explaining.
- **`COUNTRY_MAP` is small** (DE/AT/CH). Any other country → manual review.
- **The Gemini JSON fence-stripping is string hacking.** Gemini's `response_schema`/JSON mode would be more robust.
- **`flow.py` is ~1,450 lines.** It could be split per phase (`debtor.py`, `product.py`, `invoice.py`).
- **PII goes to a cloud LLM.** A local OCR/vision model is the privacy alternative.
- **Locale-dependent:** date format and `$`/`€` depend on the install and are configured in `app.yaml`.

---

## 11. Commands cheat sheet

```bash
uv sync                                   # install deps into .venv
cp .env.example .env                      # add GEMINI_API_KEY (or ANTHROPIC_API_KEY)
uv run pytest                             # 60 unit tests, ~1 s
uv run pytest -m live                     # + real LLM call

uv run fic extract data/input/order_001.png -o out.json      # image → JSON only
uv run fic run --from-json data/golden/order_001.json --dry-run  # validate, no GUI
uv run fic run data/input/order_001.png                      # full flow (Windows)
uv run fic run data/input/order_001.png --cross-check        # Gemini + Claude must agree
uv run fic probe                          # dump Fakturama's live UIA tree (debug tool)
```

Verified on 2026-09-30: `uv run pytest` → **60 passed**, and
`fic run --from-json data/golden/order_001.json --dry-run` →
"loaded + reconciled … stopping before any UI interaction" (exit 0).

---

## 12. Files to open during the interview

| If they ask about… | Open |
|---|---|
| the overall flow | `src/fic/flow.py`: `run_flow()` at the bottom |
| validation | `src/fic/models.py` and `reconcile()` in `src/fic/extraction.py` |
| LLM calls | `extract_with_claude()` / `_gemini_generate()` in `extraction.py` |
| finding controls | `resolve_by_label()` in `src/fic/uia/locator.py` |
| safe writes / save | `set_text()`, `save()` in `src/fic/uia/actions.py` |
| waiting | `src/fic/uia/waits.py` (short, easy to explain) |
| errors | `src/fic/errors.py` |
| design reasoning | `docs/design.md` |
