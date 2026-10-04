# Code walkthrough — every module, every function

This is the **function-level reference**: what each function is, what it does, and
the specific thing it handles that isn't obvious from its name. It's the companion
to the other two docs, not a replacement:

| Doc | Question it answers |
|---|---|
| [HOW_IT_WORKS.md](HOW_IT_WORKS.md) | What happens when I run it? (narrative, ~10 min read) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How is it shaped? (flow charts, call traces, stage design) |
| **CODE_WALKTHROUGH.md** (this file) | **What does this function do, and why is it written that way?** |
| [docs/uia/](docs/uia/) | The automation layer in depth — one file per `uia/` module, with interview answers |

4,982 lines of source across 16 modules. Every claim below is traceable to a line
in the repo.

---

## Contents

1. [Five rules that explain most of the code](#1-five-rules-that-explain-most-of-the-code)
2. [Module map](#2-module-map)
3. [`cli.py` — entry point](#3-clipy--entry-point)
4. [`config.py` — configuration accessors](#4-configpy--configuration-accessors)
5. [`models.py` — the domain model (tier-1 validation)](#5-modelspy--the-domain-model-tier-1-validation)
6. [`errors.py` — exception taxonomy](#6-errorspy--exception-taxonomy)
7. [`extraction.py` — image → validated data](#7-extractionpy--image--validated-data)
8. [`report.py` — run artifacts](#8-reportpy--run-artifacts)
9. [`uia/waits.py` — condition-based waiting](#9-uiawaitspy--condition-based-waiting)
10. [`uia/session.py` — attach and window management](#10-uiasessionpy--attach-and-window-management)
11. [`uia/locator.py` — the grounding engine](#11-uialocatorpy--the-grounding-engine)
12. [`uia/actions.py` — verified interaction primitives](#12-uiaactionspy--verified-interaction-primitives)
13. [`uia/grid.py` — the Items table](#13-uiagridpy--the-items-table)
14. [`uia/contact_resolver.py` — reading the database](#14-uiacontact_resolverpy--reading-the-database)
15. [`flow.py` — the orchestrator](#15-flowpy--the-orchestrator)
16. [Config files](#16-config-files)
17. [Tests](#17-tests)
18. [Questions worth being ready for](#18-questions-worth-being-ready-for)

---

## 1. Five rules that explain most of the code

If you understand these five, most individual functions become predictable. They
are the answer to "how did you handle X" for a large fraction of X.

**R1 — Never advance on an assumption; advance on a read-back.**
Every write is followed by reading the value back out of the control and comparing
it to what was intended. [`actions.set_text`](src/fic/uia/actions.py) does it inline;
[`actions.save`](src/fic/uia/actions.py#L483) verifies via the tab's dirty marker;
[`flow.complete_line`](src/fic/flow.py#L991) re-reads the grid row. A silently
rejected or coerced field edit would otherwise propagate straight into a saved
financial total with no trace.

**R2 — Ambiguity is a terminal outcome, not a thing to resolve.**
Two candidate controls within the ambiguity margin → `AmbiguousControl`. Two
debtors passing the five-field test → `ManualReviewRequired`. Two readings of the
image disagreeing on money → halt. The system never picks the more likely of two
answers when money is involved. `ManualReviewRequired` is never caught and
retried — "an ambiguous debtor is ambiguous the second time too"
([errors.py:7-9](src/fic/errors.py#L7-L9)).

**R3 — No coordinates, ever. No `sleep()` on the critical path.**
Positions are computed from the live UIA tree at the moment of the call
([locator.py](src/fic/uia/locator.py)). Waiting is always a polled predicate
([waits.py](src/fic/uia/waits.py)), never a fixed delay — "wait for the list to
stabilize" means *row contents unchanged across consecutive polls*.

**R4 — The UI is the authority on existence; the database is only advice.**
[`contact_resolver.py`](src/fic/uia/contact_resolver.py) reads Fakturama's HSQLDB
file directly, but only to decide *what string to type into the search box*. The
actual selection, and the actual proof a record exists, is always the UI dialog
plus its read-back (the brief's §2.13).

**R5 — Normalize for comparison, never for what gets typed.**
`_norm` exists in three modules ([models.py:228](src/fic/models.py#L228),
[flow.py:72](src/fic/flow.py#L72), [contact_resolver.py:104](src/fic/uia/contact_resolver.py#L104)).
All are comparison-only. What gets typed into Fakturama is always the verbatim
extracted value.

---

## 2. Module map

```
src/fic/
├── cli.py                    179   entry point: fic run | extract | probe
├── config.py                  43   YAML accessors (app.yaml, selectors.yaml)
├── models.py                 231   SourceOrder + validators  ← tier-1 validation
├── errors.py                  98   exception hierarchy + exit codes
├── extraction.py             620   image → SourceOrder      ← tier-2 validation
├── report.py                  62   runs/<id>/ artifacts
├── flow.py                  1446   the 5-phase orchestrator
└── uia/
    ├── __init__.py            21   DPI awareness at import time
    ├── waits.py              109   polled predicates
    ├── session.py            233   launch/attach, window + tab lookup
    ├── locator.py            552   grounding engine (find controls)
    ├── actions.py            614   verified interaction primitives
    ├── grid.py               196   the Items table
    └── contact_resolver.py   571   HSQLDB reader (advisory)
```

**Dependency direction** is strictly one way: `cli → flow → uia/* → errors/config`.
`models` and `errors` depend on nothing internal. `extraction` never imports
anything from `uia` — extraction is fully testable with no Windows and no
Fakturama, which is why the entire test suite runs on any OS.

---

## 3. `cli.py` — entry point

`load_dotenv()` runs at import ([cli.py:12](src/fic/cli.py#L12)) so `.env` is
read before `DEFAULT_PROVIDER` is computed on the next line — the ordering is
deliberate, so `.env` wins over a hardcoded default.

Every command imports its dependencies **inside the function body**, not at module
top. That keeps `fic --help` working without the Anthropic SDK, `pywinauto`, or a
Windows machine installed.

### `extract(image, provider, out)` — [cli.py:24](src/fic/cli.py#L24)
Runs extraction + both validation tiers and prints or writes the JSON. **No UI
involved at all.** This is the command to use when developing on Linux/macOS, or
to produce a payload for `--from-json`.

### `probe()` — [cli.py:42](src/fic/cli.py#L42)
Dumps the live UIA tree of Fakturama's main window — control type, name, rect,
enabled — one line per node. This is how `config/selectors.yaml` was calibrated
against the real app instead of guessed. Requires Fakturama already running.

### `run(image, from_json, provider, cross_check, dry_run, force)` — [cli.py:59](src/fic/cli.py#L59)
The full flow. The interesting parts are the ordering decisions:

- **`--from-json` still runs `reconcile()`** ([cli.py:112](src/fic/cli.py#L112)).
  It replaces *only* the LLM call, not the checks. It's a way to skip the model,
  not a way to skip validation.
- **The idempotency check runs before `FakturamaSession()`**
  ([cli.py:125-146](src/fic/cli.py#L125-L146)). It used to run inside `run_flow`,
  after `launch_or_attach()` — so refusing a duplicate still cost a full 1–2 minute
  Fakturama cold start before printing an answer that needed no UI at all. Reading
  the saved documents needs nothing but a file, so it belongs first.
- **`except typer.Exit: raise` comes before `except Exception`**
  ([cli.py:156-161](src/fic/cli.py#L156-L161)). Typer signals a *normal* exit by
  raising; without this clause a successful `--dry-run` was reported as "unexpected
  error" with a traceback.
- **`state = RunState()` is created before the `try`** ([cli.py:105](src/fic/cli.py#L105))
  so a mid-flow failure still renders whatever log entries were recorded, not an
  empty report.

---

## 4. `config.py` — configuration accessors

Thin, deliberately. Both YAML files are plain data — no coordinates anywhere — so
editing a label or a timeout never requires a code change.

| Function | Line | What it does |
|---|---|---|
| `app_config()` | [17](src/fic/config.py#L17) | Loads `config/app.yaml`, `@lru_cache(1)` so the file is read once |
| `selectors()` | [22](src/fic/config.py#L22) | Loads `config/selectors.yaml`, same caching |
| `money_tolerance()` | [26](src/fic/config.py#L26) | `Decimal("0.02")` — as `Decimal`, never float |
| `ambiguity_margin_px()` | [30](src/fic/config.py#L30) | 12 px — R2's refusal threshold |
| `date_format()` | [34](src/fic/config.py#L34) | `strftime` pattern **this install** expects (`%m/%d/%Y`) — a per-install setting, not a Fakturama constant |
| `payment_code_for(method)` | [40](src/fic/config.py#L40) | Closed map lookup. **Returns `None` for an unmapped method** — callers must treat that as a decision point, never guess a code |

---

## 5. `models.py` — the domain model (tier-1 validation)

The principle: **constructing an invalid `SourceOrder` should be impossible**,
not merely detected later. Money is `Decimal` end to end, never float, and
serialized as strings.

### `q2(x)` — [models.py:28](src/fic/models.py#L28)
Round-half-up to 2dp. The *one* rounding rule used everywhere money is computed,
so every computed value is reproducible and comparable.

### `SourceAddress` — [models.py:34](src/fic/models.py#L34)
`name, street, zip, city, country`, all required, all rejected if blank after strip.

### `SourceDebtor` — [models.py:50](src/fic/models.py#L50)
`customer_id_hint` is extracted from the image but **never written back** — the
brief (§2.6) requires leaving Fakturama's own proposed Customer ID alone.

**`delivery_same_as_billing`** ([models.py:70](src/fic/models.py#L70)) — a property,
not a stored flag. True only when there is no separate delivery address, *or* it
matches billing on every field that matters. A differing recipient **name** alone
makes it False, which is what triggers the second-address branch in
`create_debtor`. Comparison uses `_norm`; the typed values stay verbatim (R5).

### `SourcePayment` — [models.py:87](src/fic/models.py#L87)
`paid_status` is `Literal["PAID", "UNPAID"]` — anything else is a validation error,
not a silently-coerced default.

**`paid_requires_date`** ([models.py:101](src/fic/models.py#L101)) — a
`model_validator`, because it's a cross-field rule. PAID with no date is rejected.
The reasoning is worth quoting in an interview: the brief forbids inventing a date
(§5.3) but requires one when PAID — for such an input those two rules contradict,
and a contradiction must be surfaced, not resolved one way silently.

### `SourceItem` — [models.py:111](src/fic/models.py#L111)

Six field validators, each encoding a specific corner case:

| Validator | Line | Rule | Why not the obvious thing |
|---|---|---|---|
| `sku_not_blank` | [124](src/fic/models.py#L124) | SKU required | it's the product's identity key |
| `description_not_blank` | [132](src/fic/models.py#L132) | description required | |
| `qty_positive` | [140](src/fic/models.py#L140) | qty **> 0** | zero qty is meaningless |
| `price_not_negative` | [147](src/fic/models.py#L147) | price **≥ 0** | **zero is legitimate** (a promo/free line); only negative is rejected |
| `discount_in_range` | [155](src/fic/models.py#L155) | 0 ≤ discount ≤ 100 | |
| `vat_not_negative` | [162](src/fic/models.py#L162) | VAT ≥ 0 | 0% is a real rate |

Three computed methods:

- **`expected_line_net()`** [167](src/fic/models.py#L167) — `qty × unit_net × (1 − disc/100)`, the brief's §3.16 formula.
- **`product_master_gross()`** [172](src/fic/models.py#L172) — `unit_net × (1 + vat/100)`. Note it **ignores the line discount** (brief §3.9): the Product *master* price is not a transaction price.
- **`vat_pct_str()`** [177](src/fic/models.py#L177) — **the single most instructive function in the file.** `str(Decimal("20").normalize())` returns `'2E+1'` — scientific notation, because `normalize()` folds the trailing zero into an exponent. Fakturama parses that as **0**, so writing it created a 0%-valued tax rate *named* "VAT 20%". The name looked right, every later lookup matched it, and the error only surfaced three steps downstream as an order line showing "0 %". Affects every rate that's a multiple of ten; 19% happened to be safe, which is why it hid for so long. The fix is `format(pct.normalize(), "f")`.
- **`vat_name()`** [197](src/fic/models.py#L197) — canonical `"VAT 19%"` built *from* `vat_pct_str()`, so creation and lookup can never disagree on formatting.

### `SourceOrder` — [models.py:205](src/fic/models.py#L205)
`items: list[SourceItem] = Field(min_length=1)` — an order with no lines is invalid
by construction. `confidence: dict[str, float]` carries the model's own per-field
scores, consumed by `reconcile()`.

---

## 6. `errors.py` — exception taxonomy

Every exception carries structured `evidence` (kwargs captured into a dict) because
an operator reading `runs/<id>/report.md` needs to know **why** a run stopped, not
just that it did. `exit_code` is what the shell receives.

| Exception | Exit | Meaning |
|---|---|---|
| `AutomationError` | 1 | base class; unexpected |
| `ManualReviewRequired` | 2 | a business rule says stop — a human decides |
| `AmbiguousControl` | 2 | ≥2 grounding candidates within the margin. **Refusing to guess is the feature** |
| `OptionUnavailable` | 2 | an exact dropdown value was required and genuinely absent |
| `VerificationFailed` | 3 | a write didn't read back, or Save didn't clear the dirty flag |
| `ControlNotFound` | 4 | the grounding cascade found **zero** candidates (distinct from ambiguous: many) |
| `ExtractionError` | 5 | failed before any UI interaction — **nothing in Fakturama was touched** |
| `AlreadyProcessedError` | 6 | idempotency guard: this Cust.Ref is already saved |

`AmbiguousControl` and `OptionUnavailable` subclass `ManualReviewRequired`, so a
single `except ManualReviewRequired` catches the whole "a human must decide" class.

---

## 7. `extraction.py` — image → validated data

Two validation tiers live here, and the distinction matters:

- **Tier 1** (`parse_extraction` → pydantic): malformed *structure*. → `ExtractionError`.
- **Tier 2** (`reconcile`): the structure is fine but the *arithmetic* doesn't hold. → `ManualReviewRequired`.

Plus a third, softer pass: **completeness**, which catches valid-but-quietly-incomplete
extractions.

### `normalize_country(raw)` — [55](src/fic/extraction.py#L55)
Maps source country text to the value Fakturama's dropdown expects, via
`COUNTRY_MAP`. An unmapped country raises `ManualReviewRequired` — **never a guess**.

### `SYSTEM_PROMPT` — [66](src/fic/extraction.py#L66)
Worth reading in full. The load-bearing instructions: transcribe verbatim, never
infer; plain decimal strings; ISO dates; **billing and delivery are always extracted
separately even when they look identical**; and a specific escape hatch for a
paid-status that is neither PAID nor UNPAID (report low confidence so the pipeline
halts rather than silently mis-marking it paid).

### `_order_schema()` / `_build_tool()` / `_image_block()` — [92, 97, 105](src/fic/extraction.py#L92)
The schema is generated **from the pydantic model** (`SourceOrder.model_json_schema()`),
not hand-written. One source of truth: the model can't drift from the schema the
LLM is held to.

### `extract_with_claude(image_path, model)` — [119](src/fic/extraction.py#L119)
A **forced tool call**: `tool_choice={"type": "tool", "name": "record_order"}`.
The model can't reply with prose — it must emit data matching the schema. The
result goes through `parse_extraction` for tier-1 validation.

### `extract_with_gemini(image_path, model)` — [173](src/fic/extraction.py#L173)
Same output contract (a validated `SourceOrder`), so `self_consistency_check`
doesn't care which is "primary". Handles markdown code fences the model sometimes
wraps JSON in.

Two hard-won details:
- **The default model is an alias, not a pinned version** ([158-170](src/fic/extraction.py#L158-L170)). `gemini-2.5-pro` was hardcoded and had already been *retired* by the time a real key hit it — a 404 that looked like a config error. `-latest` aliases keep tracking.
- **`flash`, not `pro`, by default** — the pro alias's free-tier quota exhausts on roughly one image. flash extracted the sample order 46/46 fields matching the golden file.

### `_is_transient(exc)` — [237](src/fic/extraction.py#L237)
Decides what's worth retrying. The reasoning is the useful part: a 503 means the
request **never reached the model**, so retrying isn't "hoping for a different
answer to the same question" — it's re-sending a request that was refused before it
was read. Same for 429, timeouts, dropped connections. A bad key or a retired model
will fail identically forever, so those are re-raised immediately.

Last line catches httpx timeout classes whose `str()` is empty by matching on the
**exception class name**.

### `_gemini_generate(client, model, media_type, image_bytes)` — [260](src/fic/extraction.py#L260)
Bounded exponential backoff: 2s, 4s, 8s, 16s. Prints progress each attempt, because
a silent 90-second wait is indistinguishable from a hang.

The related fix is in `extract_with_gemini`: **the SDK's default request timeout is
`None` — wait forever.** A stalled request therefore never raised, so this retry
logic never fired. With an explicit `HttpOptions(timeout=...)`, a hang degrades
into an ordinary transient failure.

### `_gemini_hint(exc)` — [312](src/fic/extraction.py#L312)
Turns the three API failures actually hit during this project into one actionable
sentence each, instead of a 60-line SDK traceback. All three are configuration or
capacity problems with *different* fixes — which is exactly what a stack trace hides.

### `parse_extraction(data, source, model)` — [339](src/fic/extraction.py#L339)
The tier-1 boundary. `ValidationError` → `ExtractionError` carrying
`pydantic_errors=exc.errors()` so the report names the offending field.

### `reconcile(order)` — [351](src/fic/extraction.py#L351)
**Tier 2, and the single most important non-UI function.** Runs before a single
click. Collects *all* issues rather than failing on the first, then raises once.

Five checks:
1. **Per line**: `expected_line_net()` vs the printed `line_net_total`.
2. **Order net**: `lines − order_discount% + shipping == net_total`. An earlier version compared the bare sum of line totals against `net_total`, silently assuming discount and shipping were always zero — true of the brief's sample, false for any order that charges shipping. A correct document was rejected with a difference exactly equal to the shipping amount.
3. **VAT sum**: per line, discount-adjusted. For shipping, the rate is **inferred only when every line shares one rate**; where lines disagree it says so and halts rather than picking one.
4. **Gross**: `net + vat == gross`.
5. **Confidence**: any field the model scored below 0.80.

Never silently "fixes" arithmetic. A wrong OCR read auto-corrected to a plausible
number is the worst possible failure mode for a system writing financial records.

### `extract_and_reconcile(image_path, provider, model)` — [431](src/fic/extraction.py#L431)
**The single entry point** `cli.py` should call. Beyond the two tiers, it adds the
completeness pass:

1. Extract once (which internally reconciles).
2. If there are HIGH-severity gaps, **re-read the image** — model output is stochastic and a second reading routinely recovers what the first missed.
3. **The two readings must agree on every money field** (`money_diffs`). If they don't, the document is genuinely ambiguous → halt. Not "pick the better one".
4. If they agree, the more complete one wins.
5. Remaining HIGH gaps are printed loudly but are **not fatal by default** — a company-only order genuinely has no contact person, and refusing to process one would be wrong. `FIC_EXTRACT_STRICT=1` makes them fatal.

### `_extract_once(...)` — [509](src/fic/extraction.py#L509)
Dispatches on provider and **always reconciles**. There is no path that produces an
unreconciled order.

### `money_diffs(a, b, label_a, label_b)` — [520](src/fic/extraction.py#L520)
Every money-bearing disagreement between two extractions of the same image. Shared
by the completeness retry and the two-provider cross-check, because both need the
identical question answered: *are these two readings telling the same financial story?*

### `completeness_gaps(order)` — [547](src/fic/extraction.py#L547)
Returns `(high, low)`. Exists because of a real incident: a model swap silently
returned `first_name=None, last_name=None` for a debtor another model read as
"Elena Richter" — while still returning `elena.richter@example.test`. Structurally
valid, so nothing complained; the damage appeared much later as an unmatchable
debtor and a duplicate contact.

HIGH = it changes what the automation **does** (the five-field debtor match).
LOW = informational. Note the touch at [568](src/fic/extraction.py#L568): a missing
name is reported more urgently *if an email or phone is present*, because that means
the document does identify a person.

### `self_consistency_check(image_path)` — [587](src/fic/extraction.py#L587)
`--cross-check`. Extracts with **both** providers and diffs the money fields. Two
different models disagreeing on a total means the source image is likely genuinely
ambiguous, not just noisy. Returns the Claude extraction when they agree.

---

## 8. `report.py` — run artifacts

Artifacts are built up **continuously during the run**, not staged at the end — so
a failure mid-run still leaves a legible trail.

| Function | Line | Produces |
|---|---|---|
| `new_run_dir()` | [16](src/fic/report.py#L16) | `runs/<UTC timestamp>/screenshots/` |
| `write_extraction()` | [23](src/fic/report.py#L23) | `extraction.json` — the validated `SourceOrder` |
| `write_state()` | [29](src/fic/report.py#L29) | `state.json` — the `RunState` dataclass |
| `append_trace()` | [35](src/fic/report.py#L35) | appends one timestamped JSONL line to `trace.jsonl` |
| `render_report()` | [41](src/fic/report.py#L41) | `report.md` — outcome, error, log, embedded screenshots |

`runs/` is gitignored, deliberately: per-run screenshots and logs can contain
customer PII from the source order image.

---

## 9. `uia/waits.py` — condition-based waiting

**There is no `sleep()` on the critical path anywhere in this codebase.** (Three
short settles exist inside `grid.py`'s click sequence, where the thing being waited
for is a UI toolkit's internal click-counting state that exposes no observable
predicate.)

### `wait_until(predicate, timeout, poll, description)` — [20](src/fic/uia/waits.py#L20)
Polls until truthy, else raises `ControlNotFound` naming what it waited for.
**Exceptions inside the predicate are swallowed and retried** — a control not yet
in the tree, or a stale element reference, is a normal transient state during a
poll, not a failure. The last exception is carried into the error for diagnosis.

### `wait_stable(fn, stable_for, timeout, poll)` — [45](src/fic/uia/waits.py#L45)
Polls until the value is **unchanged for `stable_for` seconds**. This is the literal
implementation of the brief's "wait for the list to stabilize" — row count/contents
unchanged across consecutive polls, not a fixed delay.

### `wait_nested_window(root, title_pattern, timeout)` — [72](src/fic/uia/waits.py#L72)
The structural discovery that unblocked the whole dialog layer: **Fakturama's
dialogs are `Window`-type *descendants* of the main application window**, not
separate top-level OS windows. It's a single-window Eclipse RCP app. Searching
`Desktop().windows()` — the natural first guess, and what the design docs assumed —
never finds them; their OS-level title is just the generic app name.

### `wait_nested_window_gone(window, timeout)` — [100](src/fic/uia/waits.py#L100)
Works for both true top-level windows and nested ones, since `.exists()` is valid
on either.

---

## 10. `uia/session.py` — attach and window management

`uia/__init__.py` sets **DPI awareness at import time, unconditionally**, before
anything touches a window handle — otherwise UIA bounding rectangles and screenshot
pixel coordinates disagree on a scaled display.

### `find_fakturama_exe()` — [21](src/fic/uia/session.py#L21)
`FIC_FAKTURAMA_EXE` env var, then two standard install paths, then a
`ControlNotFound` that lists everything it checked.

### `FakturamaSession.__init__(...)` — [37](src/fic/uia/session.py#L37)
Two separate timeout budgets: `timeout=30` for ordinary operations,
`startup_timeout=180` for "the workspace has finished loading". A cold Eclipse RCP
start is minutes-scale; a warm attach satisfies it instantly, so a generous value
costs nothing in the common case and removes a whole class of cold-start flake.

### `launch_or_attach()` — [55](src/fic/uia/session.py#L55)
Tries `app.connect()` first (instant), falls back to `.start()`. **Prints what it's
doing and how long it will take** — a cold start blocks for a minute or more, and
printing nothing during it made a working run look frozen; an operator killed one
for exactly that reason.

The important part is `_ready_window` ([101](src/fic/uia/session.py#L101)):

> An earlier version bound `main_window` once and then polled that fixed reference
> for a toolbar. On a cold start that is a trap — Fakturama shows a splash window
> that **also** matches the title test, so the session latched onto it and waited
> the full 180s for a toolbar that would never appear inside it, while the real
> main window opened unnoticed beside it. The failure was silent by construction
> (`last_exception: None`), because the toolbar lookup returned "not yet" rather
> than raising.

The fix makes "is this the right window?" and "has it finished loading?" the **same
question**: re-find every candidate on each attempt, and the window that *has* the
toolbar is by definition the real main window. A splash can never satisfy it.

### `_candidate_windows()` — [134](src/fic/uia/session.py#L134)
Returns a **list**, not a single window, because "looks like Fakturama" isn't
sufficient to identify the real one (a leftover installer window, a splash). Excludes
anything with "install" in the title.

### `editor_tab(title_contains)` — [166](src/fic/uia/session.py#L166)
Clicks the matching `TabItem` and returns `main_window` as the resolution scope.

**This carries the project's clearest documented limitation** — worth being able to
state precisely: the `TabItem` header element does *not* contain the tab's content
fields as descendants in this UIA tree (confirmed live), so a specific tab's content
pane could not be isolated. Scoping falls back to the whole main window. That's safe
while only one editor tab is open — the only case this flow creates — because
Fakturama keeps one tab's fields enabled at a time. It stops being safe with
multiple editors open, at which point label-anchored resolution would see duplicate
labels and **correctly refuse as ambiguous** rather than guess. The failure mode
degrades safely.

### `wait_for_any_tab(*title_options, timeout)` — [203](src/fic/uia/session.py#L203)
Polls all candidate titles together within **one** timeout budget, rather than
exhausting a full per-candidate timeout in sequence. Used wherever a tab's exact
wording varies by version ("New Order" vs "Order").

### `close()` — [228](src/fic/uia/session.py#L228)
Deliberately **does not kill the process** — the operator's live session and any
unsaved manual work must not be torn down by the automation exiting.

---

## 11. `uia/locator.py` — the grounding engine

Resolves semantic descriptors against the **live** UIA tree. No coordinates are
ever written down.

### `Node` / `snapshot(container)` — [25, 37](src/fic/uia/locator.py#L25)
Flattens descendants into comparable `Node(control_type, name, rect, enabled, wrapper)`.
Per-node exceptions are swallowed — a stale element mid-walk must not fail the whole
snapshot.

### `_v_overlap` / `_h_overlap` — [59, 66](src/fic/uia/locator.py#L59)
Fractional overlap of two rects on one axis. "Same visual row" = vertical overlap
≥ 0.5. This is what makes label-anchoring robust to fields not being pixel-aligned.

### `find_by_name(container, name, control_type, exact=False)` — [73](src/fic/uia/locator.py#L73)
**Strategy S0.** Not exact by default, because Fakturama's real toolbar names carry
accelerator suffixes — `"Save (Ctrl+S)"`, `"Print (Ctrl+P)"` — so the default is
prefix matching. `exact=True` for the cases that matter.

### `_pick_anchor(anchors, pick, label)` — [98](src/fic/uia/locator.py#L98)
Chooses among several controls carrying the **same label text**. Both cases are real:
- `"last_by_top"` — the Order editor has a "VAT" label in the header *and* another in the totals block; the totals one is lower.
- `"rightmost"`/`"leftmost"` — unchecking "Delivery Address equals Invoice Address" reveals a **mirrored column** to the right with an identical set of labels. Nothing but x-position distinguishes the delivery column from the billing one.

### `resolve_by_label(container, label, want, aliases, direction, ambiguity_margin_px, pick)` — [122](src/fic/uia/locator.py#L122)
**Strategy S1, the workhorse.** Find the label text, then the nearest enabled control
of an acceptable type in `direction`, on the same visual row.

The critical five lines are [176-180](src/fic/uia/locator.py#L176-L180):

```python
if len(scored) > 1 and scored[1][0] - scored[0][0] < ambiguity_margin_px:
    raise AmbiguousControl(...)
```

If the two closest candidates are within 12px of each other, **refuse**. This is R2
made concrete, and it's the load-bearing safety property of the whole grounding
layer — independent of how many strategies exist above it.

### `resolve_anchored_icon(container, anchor_label, pick)` — [190](src/fic/uia/locator.py#L190)
For the two icon-only controls the brief calls out by position ("the upper
existing-contact icon, **not** the lower green +"). "Upper"/"lower" is computed at
runtime from the anchor's neighbourhood, never hardcoded.

Two findings baked in:
- `ICON_CONTROL_TYPES = ("Button", "Image")` — Fakturama exposes these icons as UIA **`Image`** elements, not `Button`s. `click_input()` works on either since it's a coordinate click, not an Invoke-pattern call.
- The neighbourhood filter constrains **x as well as y** ([212-221](src/fic/uia/locator.py#L212-L221)). A y-only filter is loose enough to match an unrelated control clear across a wide window — caught live when a top-right corner button matched a label near the top-left.

### `resolve_pair_by_label(container, label, want, pick)` — [231](src/fic/uia/locator.py#L231)
Returns the **two** nearest controls right of a label, left-to-right. This version's
Contact editor combines fields under one label: `"First Name Last Name"` over two
side-by-side Edits, and `"ZIP, City"` the same way — not separately labeled as the
brief's screenshots show.

### `find_nearest_right(container, anchor_rect, want)` — [264](src/fic/uia/locator.py#L264)
Like `resolve_by_label`'s scoring, but anchored on an **arbitrary rect** instead of
a text label — for fields that sit next to another *control* rather than a label.
Live case: the order header's Net/Gross price-mode dropdown has no label of its own;
it sits immediately right of the Date control. Same ambiguity refusal applies.

### `find_grid(container, required_headers)` — [290](src/fic/uia/locator.py#L290)
Locates the results grid in a dialog or list view. Two things worth knowing:

- **`required_headers` is not optional in practice.** List views are searched with `session.main_window` as scope (because the New Order tab must stay open), so the Order's own Items grid is in the tree too. Taking the first grid found would be a coin flip.
- **It's one unfiltered `snapshot()` pass, not three filtered `descendants()` calls** — a live-confirmed performance fix, not style. Against a whole editor scope the repeated filtered walks **stalled indefinitely**, and no timeout could fire because the block happens *inside a single COM call*, not between polls. The unfiltered walk over the same scope completes in a fraction of a second (~179 elements).

### `find_list_toolbar_buttons(container)` — [338](src/fic/uia/locator.py#L338)
The small add/delete buttons above a list view. **They carry no accessible name at
all** — an empty string, not `"+"` as the brief's screenshots (a green plus glyph)
and the design docs both assumed. `find_by_name(view, "+")` could therefore never
match, and both creation branches failed the moment they were first exercised.

Resolution is positional, computed from the grid's own rect: a narrow band
immediately above the grid, left-aligned with its left edge. Verified against two
different views in one session (VATs: grid left=273, buttons at 273 and 296;
Documents: grid left=422, buttons at 422 and 445) — which is what establishes it as
a layout convention rather than one screen's coincidence.

### `_header_bounds(grid)` / `_column_for(cell_rect, bounds)` — [386, 405](src/fic/uia/locator.py#L386)
`_column_for` decides which column a cell belongs to **by its left edge** —
specifically not the midpoint, and not the width:

> The "Select a product" dialog reports its Item No. cell as **790 px wide**,
> spanning essentially the whole row, while still starting exactly at the Item No.
> column and carrying that column's text. Judging by midpoint puts such a cell under
> a column three places to its right; treating width as evidence of "row decoration"
> discards it outright. An earlier version did the latter and **silently dropped
> every SKU**, so existing products looked missing and got created a second time.

Rule: the last column that begins at or before the cell (2px tolerance).

### `read_table_rows(grid, columns)` — [435](src/fic/uia/locator.py#L435)
Reads every row into a dict, **plus a `"_row"` key holding the row's pywinauto
wrapper**. Two findings:

1. **Reading a row's text is not selecting it.** Clicking OK after only reading row text silently does nothing — no item added, no error, the dialog just closes. `click_row()` uses the `_row` wrapper to actually select first.
2. **Cells are assigned to columns by position, not sequence.** Sequence is wrong in the most damaging way: a blank cell produces **no Text element at all**, so a row with empty First/Last Name yields two fewer values and everything after slides two columns left. Live symptom: a saved contact read back as `Company='Amsterdam', First Name='Vanguard Systems B.V.', ZIP=''` — so the exact-match test could never match a row that was in fact perfectly correct, and the flow created a duplicate. Position is the only thing that survives a missing cell.

### `header_columns(grid)` — [507](src/fic/uia/locator.py#L507)
Asks the grid for its own column captions. This exists because **the same bug
appeared three times** from hardcoded column lists: the VATs list was read as
`["Name","Value","Standard"]` when it's really Standard/Name/Description/Value; the
Payments list as `["Name"]` when Standard comes first — so a blank cell was compared
against a payment method name, never matched, and a duplicate was created on **every
run**. Asking the grid removes the guess.

### `click_row(row_dict)` / `row_count(grid)` — [534, 545](src/fic/uia/locator.py#L534)
Both try `DataItem`, `ListItem`, `Custom` in order, since the row control type
varies by view.

---

## 12. `uia/actions.py` — verified interaction primitives

**Every write reads back the result before the flow advances.** This module is R1.

### `escape_keys(text)` — [23](src/fic/uia/actions.py#L23)
`pywinauto`'s `type_keys()` does **not** type a plain string — it parses SendKeys
syntax, where `^ + % ~ ( ) { }` are modifiers and grouping, not literals.

Live bug: writing the VAT name `"VAT 19%"` sent **Alt+(nothing)** instead of a
percent sign, so the field read back wrong and product creation failed on its very
first field. The same trap applies to real extracted data — the project's golden
fixture carries the phone number `"+49 30 5550 1420"`, whose leading `+` would be
typed as a Shift modifier.

Rule: everything user/document-derived goes through this; deliberate key sequences
(`"{END}"`, `"{BACKSPACE 60}"`) must not.

### `wait_actionable(ctrl, timeout, description)` — [40](src/fic/uia/actions.py#L40)
Existing in the UIA tree and being usable are different things, and the gap is
measured in fractions of a second — exactly long enough to fail intermittently. A
dialog reports its children before finishing layout, so resolve-then-immediately-type
can hand pywinauto a control that raises `ElementNotVisible`. Confirmed live on the
address dialog's search box, on a path that had worked dozens of times before losing
the race once.

Every mutating primitive below waits through this first, so the readiness check
lives in one place rather than being remembered at each call site.

### `read_value(ctrl)` — [73](src/fic/uia/actions.py#L73)
A four-step fallback chain, and each step is there for a reason:

1. **`get_value()`** (UIA ValuePattern) — the correct read for Edit/ComboBox.
2. **`selected_text()`** for ComboBox.
3. **`legacy_properties()['Value']`** (MSAA bridge) — the *only* place Fakturama's segmented Date control (a `Pane`) exposes its real value; UIA ValuePattern isn't implemented on it at all.
4. **`window_text()`** — last resort.

The trap this avoids: `window_text()` reads the static accessible **Name**, and
Fakturama's Cust.Ref field's Name is permanently the string `"Cust.Ref."` regardless
of typed content. It would silently return the label forever, with no error to signal
it's the wrong property.

### `set_segmented_date(pane_ctrl, value)` — [111](src/fic/uia/actions.py#L111)
The Date field is a native 3-segment spinner exposed as a `Pane`, and it behaves
nothing like a text field:
- It is **not** free-text — typing separators or a full date string at once corrupts it into a garbled, unrelated date.
- Select-all+Delete does **not** clear it; repeated Backspace *decrements* whichever segment has spin-focus.

The only reliable recipe found: click near the control's **left edge** to focus the
month segment → 2 digits → `{RIGHT}` → 2 digits → `{RIGHT}` → 4 digits → `{TAB}` to
commit.

Read-back compares **parsed month/day/year**, not the raw string, because the legacy
display omits leading zeros (`"7/16/2026"`).

### `set_text(ctrl, value, readback=True)` — [154](src/fic/uia/actions.py#L154)
Click → clear → type → Tab → read back. Two non-obvious details:
- **`.set_focus()` alone is not enough** — some composite widgets stayed inert without a genuine `click_input()`.
- **`^a{DELETE}` does not reliably clear** Fakturama's ComboBox-composite fields; repeated writes *concatenated* instead of replacing. `{END}` + `{BACKSPACE 60}` does. Applied unconditionally rather than branching on type, since plain Edits tolerate it fine.

Tab commits, because several SWT fields validate on focus-out.

### `_combo_open_button` / `_combo_dropdown_items` — [182, 199](src/fic/uia/actions.py#L182)
Fakturama's combos are SWT `CCombo`s: a text field plus a small drop-down Button
rendered **inside** the combo's own rect, whose accessible name is literally
`"Open"` (and becomes `"Close"` when open).

The dropdown list is **not parented under the combo** in the UIA tree — it's a
sibling popup elsewhere in the window. It's identified positionally: horizontally
contained within the combo's span, vertically below it. (Same structural quirk as
the grid's transient cell editor.)

### `select_combo(ctrl, value)` — [226](src/fic/uia/actions.py#L226)
**Exact match only, never fuzzy.** A missing option is a business decision
(`OptionUnavailable` → creation branch or manual review), not something to
approximate to the nearest-sounding option.

This function contains the project's most important single lesson, and it's worth
being able to tell as a story:

> The type-and-verify fallback is actively **dangerous** on this widget, which was
> only discovered by checking the *saved record* rather than trusting the read-back.
> Typing `"VAT 19%"` into the combo's text field made `read_value()` return
> `"VAT 19%"` — so the write "verified" — but the selection never reached the model,
> the field reverted on focus-out, and the product saved with VAT **"0 %"**
> (Tax-free, the default). A silently wrong tax rate on a saved product is exactly
> the class of failure the read-back rule exists to prevent — **and here the
> read-back itself was the thing being fooled.**

`.texts()` is no help either: live-confirmed to return `['VAT', 'VAT', 'Close']` —
the label twice and the button's caption, never the options. Options only exist as
`ListItem`s once the list is open.

So the order is: **open the real dropdown and click the real option** (primary) →
if the list opened and genuinely lacks the value, raise `OptionUnavailable` **without
falling through to typing**, since typing would only fake a success → legacy
`.expand()/.select()` path → type-and-verify last, for non-CCombo controls that were
verified to work that way.

### `click(ctrl, verify_opens, timeout)` — [346](src/fic/uia/actions.py#L346)
`set_focus()` before clicking: a raw coordinate click can land on the **wrong
application entirely** if Fakturama isn't foregrounded (confirmed live, with VS Code
and a browser overlapping the same screen region). `verify_opens` waits for the
named nested dialog, so the click's *effect* is verified, not just its dispatch.

### `click_menu_item(window, menu_name, item_name, timeout)` — [369](src/fic/uia/actions.py#L369)
The route used for **all four** creation branches (New Contact / Payment / VAT /
Product), because the obvious route — the `+` button above each list — turned out to
be unusable: those buttons expose no name, no automation id, no help text and no
MSAA description (anonymous `Role 43` push buttons), and clicking one produced only
a transient tooltip, never an editor.

The non-obvious handling: **Fakturama's menu items exist in the UIA tree even while
their menu is closed**, reporting a degenerate `(0,0)` rectangle. Clicking one in
that state clicks the **screen origin** — i.e. some other application. So this opens
the parent menu, then waits for the target item to acquire a **non-zero rect**,
which is the actual signal the menu is open.

### `snapshot_top_level_handles()` / `check_for_new_dialog(before)` — [413, 431](src/fic/uia/actions.py#L413)
Baseline-and-diff detection for an unexpected native dialog appearing as a side
effect of **any** action. Fakturama's duplicate-contact check can fire on tab-out of
the **Street** field mid-form, not only at Save — and the dialog then steals focus,
silently corrupting whatever is typed next (confirmed live: a `"Berlin"` write landed
as `"B"`, truncated when the popup grabbed focus mid-keystroke).

The tooltip filter in `check_for_new_dialog` is load-bearing, not tidiness: SWT
renders tooltips as genuine new top-level windows with empty titles, so a plain "any
new window" test raised *"Save triggered a dialog"* on saves that were fine — a false
positive that aborts a good save and sends the operator to inspect a dialog that has
already vanished. Rule: a real SWT MessageBox has **either a title or buttons**; a
tooltip has neither. Conservative in the other direction (untitled + buttons still
counts), because missing a real modal is what caused the original hang.

### `save(editor_window, save_button, tab_title)` — [483](src/fic/uia/actions.py#L483)
Clicks Save **once** (the brief repeats this in every stage) and verifies. Four
distinct findings live in this one function:

1. **The dirty marker is on the `TabItem`, not the window.** SWT marks an unsaved tab with a leading `*` — `"*New Order"`, `"*New Contact"`. An earlier version checked `editor_window.window_text()`, which in this codebase is always `session.main_window` and never carries the marker — so the dirty check **passed immediately on the first poll, verifying nothing.** Caught only by noticing a fix didn't change behaviour when it should have.

2. **`tab_title` scopes the check to one tab, and that scoping is load-bearing.** An earlier version asked "does *any* tab carry a `*`" — wrong in precisely the situation this project is always in: the brief requires the Order tab to stay open, and it's dirty from the moment its header is set. So while saving a Contact, the Order's `*` was always present, `_still_dirty()` could never go False, and every save was reported as failed. That was previously misread as too tight a timeout and "fixed" by raising 15s→30s, which changed nothing except how long it took to fail.

3. **The target tab is activated before clicking Save**, because the toolbar Save button acts on whichever editor is **active** — not on `editor_window`. Confirmed live: creating a Contact triggers the payment-method sub-branch, which opens and saves a Payment editor; that tab was left active, so the next Save saved *it* again and the Contact stayed dirty until timeout.

4. **The duplicate-contact dialog is a true top-level window**, not a nested one like every other Fakturama dialog — a native SWT MessageBox titled generically "Fakturama". An earlier version only checked nested descendants and never detected it: the dirty check then polled forever against a tab that could never un-dirty while a blocking modal sat un-clicked, and **the whole run hung rather than failing.** Detection now diffs true top-level windows, and the 30s outer timeout is a hard ceiling regardless — so a miss degrades to a bounded failure, never another silent hang.

Any dialog during Save → `ManualReviewRequired`. Clicking through blind risks
confirming a save Fakturama itself flagged as a likely duplicate.

---

## 13. `uia/grid.py` — the Items table

This was **the single biggest open question in the whole design**, and both halves
are now answered live.

**Reading**: the grid is a real, fully accessible `List` of `ListItem` rows with
ordinary `Text` cell children. Not canvas-drawn, not NatTable — confirmed against
Fakturama's own source (`DocumentEditor.java`): a standard JFace `TableViewer` with
a `DocumentItemEditingSupport` providing a `TextCellEditor` per numeric column.

**Writing**: the activation pattern is **two separate single clicks** — click the
row (selects it), then a second discrete click on the target cell — matching JFace's
`MOUSE_CLICK_SELECTION` activation event.

That is **not** a double-click (`double_click_input()` sends one `WM_LBUTTONDBLCLK`
message, which JFace's click-counting listener does not treat as two discrete
clicks) and **not** F2. Six patterns were tried and ruled out before this one. The
winning pattern was found by **reading Fakturama's actual source** for its
editing-support class, which confirmed a plain `TextCellEditor` and narrowed the
search from "guess at a custom widget's private protocol" to "find the right JFace
click sequence".

| Function | Line | What it does |
|---|---|---|
| `probe_grid_accessibility(region)` | [55](src/fic/uia/grid.py#L55) | Does the grid expose addressable rows? Tries `ListItem`, `DataItem`, `Custom`, `Edit` — the original guesses kept alongside the confirmed answer, since another version could differ |
| `read_line_values(row)` | [70](src/fic/uia/grid.py#L70) | Every cell left-to-right, mapped onto `COLUMN_ORDER` |
| `numeric(text)` | [79](src/fic/uia/grid.py#L79) | Strips `"5.70 $"` / `"10 %"` to magnitude |
| `_snapshot_edit_rects` / `_find_new_edit` | [107, 95](src/fic/uia/grid.py#L95) | Before/after diff to find the transient cell editor |
| `set_cell_via_children(scope, row, column, value)` | [118](src/fic/uia/grid.py#L118) | The write primitive |
| `learn_column_order` / `set_cell_via_keyboard` | [179, 187](src/fic/uia/grid.py#L179) | Canvas-path fallback — **`NotImplementedError`**, explicitly kept for a hypothetical canvas-drawn version |

**`numeric()` is worth understanding**: Fakturama displays Discount as a **negative**
percentage (`"-10 %"` for a typed `10`) — a display convention (a discount is a
negative price adjustment), not a sign error in the write. None of this project's
grid fields are ever meaningfully negative, so dropping the sign is correct here,
not merely convenient.

**`set_cell_via_children`**: the transient editor is a genuine `Edit` element but is
**not parented under the row or the List** — hence `scope` being the broader editor
window, and hence the before/after rect diff. Reads back from the **row's own cell
text** post-commit (the editor is gone by then), compared numerically since
Fakturama redisplays with its own formatting and suffix.

The independent cross-check that proved the write reaches the real data model:
writing `"3"` into a `Qty=1` line changed it to `Qty=3` **and** recalculated
`Price: 1.90 $ → 5.70 $`.

---

## 14. `uia/contact_resolver.py` — reading the database

**Read R4 first.** This module is advisory. It reads Fakturama's HSQLDB `.script`
file to decide *what to type into the search box* — never to decide whether a
record exists.

It exists because of two live-confirmed limitations:
1. **Fakturama's search does single-string substring matching against one column at a time**, with no cross-column AND and no tokenization. `"Ahmed Ali"` only matches if that exact substring is in ONE column — it fails whenever first and last name live in separate `FIRSTNAME`/`NAME` columns, which is the normal case.
2. **The displayed Customer ID (`NR`) is not a reliable unique key.** Confirmed in this project's own test data: force-restarting Fakturama (a `taskkill`, not a clean shutdown) caused it to **reissue an already-used `NR`** for a genuinely different contact. Fakturama's own native duplicate check exists precisely because it can't fully trust its own `NR` either.

### `normalize_arabic(text)` / `_norm_field(text)` — [89, 104](src/fic/uia/contact_resolver.py#L89)
Folds alef/hamza/taa-marbuta/alef-maqsura variants to one canonical form, strips
tatweel and diacritics, collapses whitespace, casefolds. Applied **uniformly** rather
than branching on script, because Arabic folding is a no-op on non-Arabic text.
Comparison-only (R5).

### `ContactRecord` / `ResolveResult` / `ContactResolverError` — [117, 136, 153](src/fic/uia/contact_resolver.py#L117)
`ContactRecord.id` is the real HSQLDB primary key — **the only field safe to treat
as unique**. `nr` is carried for display/logging only.

`ResolveResult` is designed so exactly one branch drives the caller: 0 matches →
safe to create; 1 → reuse; >1 → manual review. `duplicate_nr_groups` is a
**diagnostic**, surfaced so the `NR` integrity problem is visible in logs even when
it doesn't affect this particular call.

`ContactResolverError` is deliberately **not** a `fic.errors` type — it's a resolver
*internal* failure (file missing/unreadable/unparseable), distinct from the caller's
business-level response to an ambiguous result.

### `find_script_file(candidates)` — [164](src/fic/uia/contact_resolver.py#L164)
Note the comment on `DEFAULT_SCRIPT_CANDIDATES`: the real path is
`~/Database/Database.script`, **not** the `%USERPROFILE%\Fakturama2\` path this
project's own earlier config comments assumed — that was never verified against a
real install and turned out to be wrong.

### `_column_order_from_ddl(script_text, table)` — [175](src/fic/uia/contact_resolver.py#L175)
Parses the `CREATE MEMORY TABLE` line at **runtime** to get the real column order,
rather than trusting a hardcoded list a different version could silently invalidate.
Depth-tracking comma split, because `VARCHAR(256)` contains a comma-free paren but
other types don't. Falls back to `FALLBACK_CONTACTS_COLUMNS` if the DDL isn't found.

### `_split_sql_values(values_str)` — [204](src/fic/uia/contact_resolver.py#L204)
Tokenizes a `VALUES(...)` body respecting SQL single-quote literals, including `''`
as an escaped quote. A naive comma-split breaks the moment any text field (a `NOTE`)
contains a comma.

The trailing-field handling at [239-245](src/fic/uia/contact_resolver.py#L239-L245)
is subtle and worth knowing: the last field is **always** appended, even when empty.
A bare `,''` last column has nothing accumulated, and an `if current:` guard would
silently drop it — desyncing every column read after it.

### `parse_contacts(script_path)` — [256](src/fic/uia/contact_resolver.py#L256)
**Also reads `Database.log`.** HSQLDB appends new rows to the write-ahead log and
only folds them into `.script` at a checkpoint (a clean shutdown) — so a contact
created *minutes* ago, **including one this very run just created**, is invisible in
`.script` alone. Confirmed live: after creating a contact, the resolver still
reported the older one as the only match.

Malformed rows are skipped, not fatal — one bad row must not take down the whole
resolve.

### `resolve(company, first_name, last_name, zip_code, city, street, script_path)` — [330](src/fic/uia/contact_resolver.py#L330)
**Two tiers.**

**Tier 1 — exact company name.** Case- and whitespace-insensitive, never partial:
`"Northstar Office GmbH"` matches `"  northstar office GMBH  "` and does **not**
match `"Northstar Office GmbH FreshTest"`.

This tier exists because tier 2 alone is too eager to declare ambiguity. Confirmed
live: three contacts sharing one person and street but with clearly *different*
companies were all reported as candidates by the name+street mirror, halting a run
whose correct answer was never in doubt.

Reusing on company alone is safe **for a specific reason worth stating out loud**:
this function only chooses which string to type into the search box. The actual
selection is still gated by `try_select_debtor`'s five-field exact match against the
real dialog rows — so a tier-1 hit can never by itself cause the wrong customer to
be billed.

**Tier 2 — the brief's §2.3 rule**: AND-match across Company/First/Last/ZIP/City,
plus (when `street` is given) any row matching on `(first, last, street)` alone. That
second rule **mirrors Fakturama's own native duplicate check**, so the caller can
route to manual review *before* opening a dialog that check would otherwise block on.

### `document_number_exists(number, script_path)` — [440](src/fic/uia/contact_resolver.py#L440)
Fakturama proposes the next document number from a counter persisted only on **clean
shutdown**, so an unclean exit leaves the counter behind the data and it re-proposes
a number that already exists. Saving then fails with its own dialog — but only at the
very **end**, after the order is fully built, every product created and all totals
verified. This check turns several minutes of wasted work into an instant, actionable
failure.

Deliberately a plain substring test over both files rather than a `DOCUMENTS` parse:
the quoted number is unique enough not to false-positive, it needs no column layout,
and a miss is harmless (Fakturama's own dialog still catches it).

### `_read_db_text(script_path)` — [478](src/fic/uia/contact_resolver.py#L478)
`.script` + `.log` concatenated, for the same checkpoint reason as `parse_contacts`.

### `find_orders_by_reference(external_reference, script_path)` — [497](src/fic/uia/contact_resolver.py#L497)
**The idempotency check.** Re-running on the same order image must not silently
produce a second Order.

Two deliberate decisions:
- The comparison is on the **parsed `CUSTOMERREF` column**, not a file-wide substring search — a substring would also match the reference appearing in an address, a note, or another document type, and would refuse to run for the wrong reason.
- **Deduplicated by document number.** HSQLDB's log records a fresh INSERT each time a row is rewritten, and the row may also be checkpointed in `.script` — reporting `"PO000016, PO000016, PO000016"` would misrepresent one document as three.

Returns dicts (number, ref, type) so the caller can **name what it found** rather
than just refusing. If the DB can't be read at all it returns `[]` — advisory:
never block the run on the check's own failure.

### `search_key_for(record)` — [563](src/fic/uia/contact_resolver.py#L563)
Returns the **last name** (falling back to company). Not `NR` (unreliable), not
company (multi-word, punctuation-heavy — exactly the fragile case). Fakturama's
search matches a single word against its column correctly; the failure mode is
specifically multi-word queries spanning two columns, which searching by last name
sidesteps entirely.

---

## 15. `flow.py` — the orchestrator

Five phases, brief steps 1–5. Each step function: **acts → waits for the UI to
settle → reads back what persisted → compares to `source` → advances or raises.**

### `RunState` — [45](src/fic/flow.py#L45)
Threaded through every step and written to `runs/<id>/state.json` after each
transition, so a mid-run failure leaves a legible trail of exactly how far it got.

`note(msg)` ([61](src/fic/flow.py#L61)) appends **and prints immediately**. The log
used to be collected silently and shown only at the end — which meant a run could
print nothing for minutes, indistinguishable from a hang, and confirmed live to make
an operator kill a run that was working fine. *Progress that only appears after
success is no help during the part where you actually need it.*

`_loggable_rows(rows)` ([76](src/fic/flow.py#L76)) strips the `_row` pywinauto
wrapper before a row dict goes into a log message or exception evidence — that
evidence gets serialized to JSON, and a live COM wrapper has no business there.

---

### Phase 1 — `open_order(session, source, state)` — [90](src/fic/flow.py#L90)

Brief §1.3–1.8. Returns the editor container; the tab stays open for the whole run.

| Step | How | The catch |
|---|---|---|
| Click Order | `find_by_name("Create: New Order", Button)` | the real toolbar name, not bare "Order" |
| Read No. | `resolve_by_label("No.")` | **read only** — never changed (§1.4) |
| Pre-check the number | `document_number_exists()` | see below |
| Set Date | `find_by_name("Date", Pane, exact=True)` | resolved by **name**, not label |
| Set Cust.Ref | `resolve_by_label("Cust.Ref.")` | alias `"Kundennr."` for a German install |
| Set price mode | `find_nearest_right(date_rect, ComboBox)` | **no label exists** for this field |
| Set VAT mode | `resolve_by_label("VAT", ComboBox)` | |

**The proposed-number pre-check** ([113-130](src/fic/flow.py#L113-L130)) fails fast
if Fakturama proposed an already-used number. The brief forbids altering it, so this
can't be auto-fixed — but it can be reported *now* rather than after the entire order
is built. The error message names the actual cause (force-killing Fakturama instead
of File > Exit) and the fix. Confirmed live twice.

**Date is resolved by name, not by label** — and that's a bug fix, not a preference.
Label-anchored search landed on the unrelated Net/Gross dropdown immediately to its
right, and an earlier version **wrote dates into that field**. Meanwhile the price
mode field has no label of its own, so *it* has to be found positionally relative to
Date. The two resolutions are mirror images of each other's failure.

---

### Phase 2 — the Debtor

Five columns define an exact match ([175](src/fic/flow.py#L175)):
`Company, First Name, Last Name, ZIP, City`. Note `DEBTOR_DIALOG_COLUMNS` has an
**extra leading "Customer ID" column** and calls the surname column **"Last Name"**,
not "Name" as the brief's prose implies.

#### `_debtor_wanted` / `_debtor_exact_matches` — [178, 188](src/fic/flow.py#L178)
Build the wanted dict; return every row where **all five** normalized fields match.

#### `_debtor_mismatch_report(rows, debtor)` — [197](src/fic/flow.py#L197)
For each candidate row, **exactly which of the five fields disagreed**.

> *"The exact-match search and the just-written values disagree"* is a true statement
> and a useless one — it was the entire error message when a real run failed, and
> answering "disagree HOW?" meant reading the database by hand. The comparison
> already knew which field differed; it just wasn't saying. Reporting it turned a
> live incident into a five-second diagnosis: the extraction had no first/last name,
> so it was comparing `''` against `'Elena'`/`'Richter'`.

#### `_resolve_search_key(debtor, state)` — [221](src/fic/flow.py#L221)
Calls the DB resolver to pick a better search string. **Fails soft in every
direction**: a `ContactResolverError`, or literally any other exception, falls back
to searching by company. A resolver failure degrades search precision; it never
blocks the flow. The one case that *does* raise is >1 DB match — genuinely ambiguous
before the UI is even opened.

#### `try_select_debtor(editor, source, state, search_key, confirming)` — [286](src/fic/flow.py#L286)
Brief §2.1–2.4. Returns `True` if an existing debtor was selected, `False` if the
creation branch is needed.

Note the definition of ambiguous: **more than one row PASSES THE EXACT-MATCH TEST** —
not that the raw search returned more than one row.

Three outcomes:
- **exactly 1 exact match** → `click_row()` (selecting, not just reading — see §11), OK, wait for the dialog to close, return True.
- **>1** → `ManualReviewRequired`.
- **0** → Cancel, record the mismatch report, then the guard below.

**The cross-check guard** ([365-375](src/fic/flow.py#L365-L375)) is the most valuable
few lines in the phase:

> The database says this company already exists, but the UI's five-field test
> rejected every row. Those two cannot both be right, and creating a customer anyway
> is the one outcome that does lasting damage: a duplicate record splits a real
> customer's history and invoices.
>
> Live case this prevents: an extraction returned no first/last name, so the
> five-field test could never match the stored contact, and the run cheerfully
> created a second "EuroTech Solutions GmbH" with blank names. The resolver had
> already reported the real one. **Nothing was broken except that nothing compared
> the two answers.**

`confirming=True` exempts the post-creation re-selection, where the resolver
legitimately matches the contact just made.

#### `create_debtor(session, editor, source, state)` — [384](src/fic/flow.py#L384)
Brief §2.5–2.11. The Order tab stays open throughout.

**This version's Contact editor does not match the brief's screenshots**, and the
function documents each difference:
- `"First Name Last Name"` and `"ZIP, City"` are **combined labels** over two side-by-side Edits → `resolve_pair_by_label`.
- Country is **plain free text**, not a dropdown.
- There is **one address block** with a single `"Delivery Address equals Invoice Address"` checkbox, not the brief's separate per-address Invoice/Delivery role checkboxes.
- E-Mail and Telephone aren't on the Address sub-tab at all — attempted defensively, a miss is **logged, not fatal**, since neither is required by validation nor gated on by the brief.

**The Street guard** ([426-436](src/fic/flow.py#L426-L436)) — snapshot handles
before, check immediately after — because Fakturama's duplicate check fires on
tab-out of Street and steals focus mid-form.

**The differing-delivery branch** ([465-498](src/fic/flow.py#L465-L498)): unchecking
reveals a **mirrored column to the right** with an identical label set, so every
write pins `pick="rightmost"`. The billing fields above are written *before* the
uncheck, while only one column exists, so they can't be caught by the same ambiguity.
`SourceAddress` carries one combined `name` while the UI splits First/Last/Company —
the combined name goes into the delivery **Company** field, because splitting a name
the source gave whole would be inventing structure the document never stated.

**The payment sub-branch** ([523-542](src/fic/flow.py#L523-L542)) — try to select;
on failure call `ensure_payment_method`, then **re-acquire the tab and re-resolve the
control**, because that function navigates away and saves its own editor. Reusing the
stale control would write into whatever is active now.

Ends by returning to the Order and **re-selecting** — successful selection *is* the
save confirmation (§2.13), never a DB peek. Searches by the company just written, not
the resolver's suggestion, because the resolver may point at a different contact and
the one needed is the one that did not exist a moment ago.

#### `_check(checkbox, checked)` — [572](src/fic/flow.py#L572)
Uses UIA's **TogglePattern**, not `read_value` — the latter's fallback chain would
return the checkbox's own label text, which is never a signal of checked-vs-not.

The state is **polled**, not read once: SWT reports the **pre-toggle** value on a read
taken immediately after `toggle()`, so a single read fails on a toggle that in fact
worked. This bit the Invoice's "paid" box specifically, where the click also triggers
a panel re-layout.

#### `_derive_alias(debtor)` — [611](src/fic/flow.py#L611)
The brief has no fallback rule for a missing Alias. Deterministic derivation —
`COMPANY-CITY`, upper-cased, slugged — rather than an ad-hoc guess, so the same
debtor always produces the same alias.

#### `ensure_payment_method(session, method, state)` — [630](src/fic/flow.py#L630)
Brief §2.10. Search the Payments view → reuse if exactly one exact match → else
create via `New > New Payment`.

**Where the unmapped-code decision moved, and why**, is a good judgement story:
this function used to refuse up front for any method missing from
`payment_code_map`, before touching the UI. That gate **protected nothing on this
build** — its Payment editor has no payment-code field at all, so the mapped code is
never written anywhere, yet an ordinary method like "ACH Transfer" was blocked
because a lookup table lacked a value that could not have been used. Now an unmapped
method **never halts**: the map is a convenience for pre-filling a code, not an
allow-list of methods, because Fakturama records the method *name* and the Invoice
selects by that name. If the editor exposes the field and there's a mapping, the code
is selected; if there's no mapping, the field keeps its default and the run notes
it; if the field is absent, the missing mapping is irrelevant and merely logged.

Everything between Name/Description and Save is **best-effort by deliberate design**,
because a half-filled optional field must never cost the save. Live: an exception on
the payment-code lookup left a `*New Payment` editor open, filled in and never saved
— so the method didn't exist when Phase 5 went looking, and the run failed at the very
end. The method had been typed in; nothing had committed it.

`Cash discount`/`Discount Days`/`Net Days` are compared **numerically** and written
only if actually different — they already default to zero and redisplay with units
(`"0 %"`), so a plain write+read-back reports a false mismatch on a correct value.

---

### Phase 3 — Products, per line, in source order

#### `_open_product_dialog_and_search(editor, sku)` — [757](src/fic/flow.py#L757)
The anchor is the **"Items" label beside the icon column** — not a table header. The
inline grid's header cell reads "Item No."; this icon sits outside that grid entirely.

#### `resolve_product_line(session, editor, item, state)` — [772](src/fic/flow.py#L772)
Search by exact SKU → >1 is `ManualReviewRequired` → 0 triggers
`ensure_vat` then `create_product`, then **re-acquires the Order tab** and searches
again. `click_row` before OK, always.

**The settle wait** ([808-824](src/fic/flow.py#L808-L824)) is a real condition, not a
sleep: poll until a grid row actually carries this SKU. Grid cell-edit activation is
timing-sensitive, and the row-then-cell click sequence that works in isolation failed
when run immediately after the dialog closed.

#### `_vat_value_matches(displayed, pct)` — [851](src/fic/flow.py#L851)
Compares a VAT row's displayed value **numerically**. String comparison was wrong in
both directions: it had to know Fakturama's display formatting, *and* it was fed
`str(pct.normalize())`, which renders 20 as `"2E+1"` and so could never match
anything.

#### `ensure_vat(session, item, state)` — [869](src/fic/flow.py#L869)
Brief §3.4–3.6 — resolved **before** New product, so the rate is available in the
product's VAT dropdown.

`VAT_LIST_COLUMNS = ["Standard", "Name", "Description", "Value"]` — the comment above
it ([835-842](src/fic/flow.py#L835-L842)) describes **two bugs stacked, each hiding
the other**: the original `["Name","Value","Standard"]` was wrong in membership *and*
order, so the existence check compared the Standard column's blank marker against the
wanted VAT name and could never match any row; every run therefore fell through to
the creation branch — which then failed on its own separate `+`-button bug.

Fakturama calls this editor a **"TAX Rate"**, not a "VAT" — waiting for a tab named
"VATs" (the *list view's* name) would never match.

**The Value is read back before saving** ([926-939](src/fic/flow.py#L926-L939)). This
is the guard that would have caught the `2E+1` bug at the point of writing instead of
three steps later as a mystifying *"line VAT '0 %' does not match extracted 20%"*.

#### `create_product(session, item, state)` — [951](src/fic/flow.py#L951)
Brief §3.7–3.11. **Three of this function's labels were wrong** against the real
editor and only surfaced when the creation branch was first exercised live:
- `"Price"` → the real label is `"Price (gross)"` (and the Edit beside it has no accessible name of its own, so the label is the only route to it).
- `"Stock"` → the real label is `"Quantity"`.
- `"cost price"` → **no such field exists on this editor at all.** Dropped rather than guessed at another location.

"Quantity" here is the product master's stock level, not the order line's quantity.
Left at 0 deliberately — the brief supplies no stock figure, and inventing one would
write a fact the source document never stated.

#### `complete_line(editor, item, state)` — [991](src/fic/flow.py#L991)
Brief §3.13–3.16.

**The Items grid is identified by its header content, not by index**
([1008-1018](src/fic/flow.py#L1008-L1018)): `editor.descendants(control_type="List")`
can return more than one List, and blindly taking `[0]` silently picked an empty list
elsewhere in the tree in at least one live run — making `probe_grid_accessibility()`
report "canvas-drawn" for a grid that is fully accessible.

Then: set Qty → **cross-check** U.Price (write only if different) → **VAT mismatch is
a halt, not a write** (if the line's VAT disagrees with the extraction, the
selected/created product's VAT is wrong and that's a human's call) → set Discount →
verify the resulting line Price against `expected_line_net()`.

---

### Phase 4 — `complete_and_save_order(session, editor, source, state)` — [1074](src/fic/flow.py#L1074)

The totals block, live-read from a real 2-line order:
`Total Net 17.30 | Discount 0% | Shipping 0.00 | VAT 3.29 | Total 20.59`.

**The `"Total Net"` vs `"Total Gross"` fallback is the tail of a genuinely
instructive bug** ([1080-1094](src/fic/flow.py#L1080-L1094)):

> An earlier version resolved "Total Gross" and compared **both** it and "Total"
> against `net_total` — and that "worked" only because the order was silently stuck
> in **Gross** mode: `select_combo`'s old type-and-verify path never committed the
> "Net" selection (the same failure later found on the product VAT combo). That also
> fully explains what had been logged as an unresolved VAT discrepancy: Fakturama
> showed 1.09 where the formula gave 1.30, because `6.84 / 1.19 × 0.19 = 1.09` — it
> was computing the VAT *contained in* a figure it considered gross. With the combo
> fixed, the mode is really Net and VAT reconciles exactly.

**Shipping has to be written** ([1129-1148](src/fic/flow.py#L1129-L1148)) — it is not
implied by the item lines. Found by running an order that charges shipping: every
line was correct, yet all three totals came up short by exactly the shipping amount,
because Fakturama was never told about it. The brief's own sample ships free, which
is why this stayed invisible until a second document exercised it.

**Two different net conventions, compared on purpose**
([1150-1161](src/fic/flow.py#L1150-L1161)): Fakturama's "Total Net" is the *items*
net and **excludes** shipping, even though its "Total" includes it. `SourceOrder.net_total`
uses the other common convention and counts shipping in. Rather than force them to
look alike, the items net is derived from the lines independently — and `reconcile()`
has already proven the two agree.

The order-level Discount is **checked, not assumed** — a stray discount would silently
change the saved total while every per-line check still passed.

### `followup_invoice(session, order_editor, state)` — [1206](src/fic/flow.py#L1206)
Brief §4.6 — create the Invoice **from** the saved Order so the two stay linked,
never via the toolbar (which would start an unlinked blank document).

**The mechanism is not what the brief describes.** There is no "Create a follow-up
document" text in this build. It's a `Group` named **"Create a duplicate"** inside
the saved Order editor, holding four buttons: Confirmation / Invoice / Delivery Note
/ Proforma. Resolution is scoped to that Group deliberately — a bare `"Invoice"` name
would plausibly match elsewhere, and scoping documents *which* of the four follow-ups
the brief wants.

### Phase 5 — `complete_and_verify_invoice(...)` — [1247](src/fic/flow.py#L1247)

1. **Verify the inherited totals** rather than trusting the copy. Confirmed live that all three come across intact — which is exactly why checking is cheap and a silent divergence would otherwise be invisible.
2. **Find the payment combo positionally** ([1305-1315](src/fic/flow.py#L1305-L1315)): it has **no label and no accessible name**, so `resolve_by_label(..., "Payment")` finds nothing. It's the ComboBox immediately right of the "paid" checkbox, and it defaults to "Pay Cash" — so it genuinely has to be set, never already correct by luck.
3. **No creation branch here** (§5.2) — an Invoice offering no such method is a human decision, unlike the Debtor's payment method.
4. **If PAID**: tick `paid`, *then* resolve the date and value fields — **ticking replaces the panel's contents**. The unpaid layout shows "Due Days" + "Pay Until"; only once paid is ticked do the two fields the brief wants appear: a date `Pane` named **`"at"`** (not "payment date", which exists nowhere) and an Edit named `"Value"`. They must be resolved *after* the tick, never before.
5. **`Value` is confirmed, not blindly overwritten** — it comes prefilled with the invoice total, so a disagreement surfaces instead of being papered over by the write.
6. **If not PAID**: leave it clear. No date or value invented (§5.3).
7. Save. **Flow ends** — no Delivery, Correction or Dunning document (§5.7).

### `run_flow(session, source, force)` — [1373](src/fic/flow.py#L1373)
The five phases in order, with the idempotency pre-flight first.

The one non-obvious step is at [1437-1443](src/fic/flow.py#L1437-L1443): calling
`ensure_payment_method` **before** creating the Invoice. It's idempotent (it searches
first and returns immediately if the method exists) but it is **not redundant** —
§2.10's creation branch only runs while *creating* a debtor, so an order for an
**existing** customer whose payment method was never created would reach §5.2 and
fail with "required payment method not available", having had no opportunity to
create it. Confirmed live on exactly that path.

---

## 16. Config files

Both are plain data. **Zero coordinates**, which is what makes "no hardcoded layout"
inspectable rather than merely claimed.

### `config/app.yaml`
Install paths, `locale.date_format` (per-install, not a Fakturama constant — verify
with `fic probe` on a different box), timeouts, `grounding.ambiguity_margin_px: 12`,
`matching.money_tolerance: "0.02"`, and `payment_code_map`.

The map's comment is worth reading: entries are only the ones justifiable **from the
payment instrument itself**. Anything genuinely ambiguous is deliberately absent
rather than guessed — a wrong payment code is a wrong financial record.

### `config/selectors.yaml`
The readable contract between `flow.py` and Fakturama's UI. Beyond the descriptors
themselves it carries three kinds of annotation:
- `expect:` — a post-condition asserted after a write.
- `readonly_intent` / `never_write` / `never_click` — the brief's prohibitions, encoded as data (proposed No., proposed Customer ID, Standard VAT, "Set as standard").
- `scope:` — disambiguates labels that legitimately appear twice in one editor.

Several entries carry inline notes recording where the live UI diverged from the
brief — useful evidence that the calibration was done against the real app.

---

## 17. Tests

782 lines across four files. `conftest.py` puts `src/` on `sys.path` so `import fic`
works with no editable install.

**All three run on any OS** — none needs Windows, Fakturama, or an API key. That's a
direct consequence of `extraction`/`models` never importing `uia`.

| File | Lines | Covers |
|---|---|---|
| `test_models_and_reconcile.py` | 194 | golden-fixture parsing, the gross-price canary (297.50 / 47.60), every tier-1 validator's accept **and** reject case, tier-2 tolerance behaviour, `q2` half-up rounding, and a parametrized `vat_pct_str` test that locks the `2E+1` bug shut |
| `test_contact_resolver.py` | 507 | `.script` parsing (real column layout, embedded commas, deleted rows), both resolve tiers, the multi-word-name-across-columns case, name+street collisions, duplicate-`NR` detection, and five Arabic normalization tests |
| `test_extraction_golden.py` | 74 | extraction against the golden files |

The reject-case tests are the interesting half: `test_zero_price_line_accepted` and
`test_negative_price_rejected` sitting next to each other is the whole "zero is a
legitimate promo line, negative is not" decision, pinned.

---

## 18. Questions worth being ready for

Short answers, each with a code anchor.

**"How do you find controls without hardcoded coordinates?"**
Three strategies over a live UIA tree snapshot: S0 exact name+type
([`find_by_name`](src/fic/uia/locator.py#L73)), S1 label-anchored spatial resolution
([`resolve_by_label`](src/fic/uia/locator.py#L122)), and anchored-icon resolution for
the two icon-only controls the brief names ([`resolve_anchored_icon`](src/fic/uia/locator.py#L190)).
Positions are computed at the moment of the call. Ordinal traversal (S2/S3) and a
vision fallback (S4) are designed but **not implemented** — that's in the README's
skipped list.

**"What stops it from picking the wrong control?"**
The ambiguity margin: two candidates within 12px of each other → `AmbiguousControl`,
refuse. It's five lines ([locator.py:176](src/fic/uia/locator.py#L176)) and it's the
load-bearing safety property of the whole layer, independent of how many strategies
sit above it.

**"How do you know a write actually worked?"**
Read it back and compare before advancing (R1). And the honest follow-up: **the
read-back can itself be fooled** — `select_combo`'s story
([actions.py:253-267](src/fic/uia/actions.py#L253-L267)), where a combo's text field
returned the typed value while the selection never reached the model and the product
saved with VAT 0%. That's why combo selection now clicks a real dropdown option, and
why `ensure_vat` re-reads the Value before saving.

**"What happens on ambiguity?"**
It halts, with structured evidence naming the candidates. `ManualReviewRequired` is
never caught and retried — exit code 2. An ambiguous debtor is ambiguous the second
time too.

**"How do you avoid creating duplicate customers?"**
Three independent guards: the five-field exact match against the dialog's real rows;
the DB resolver cross-check that refuses to create when the database says the company
exists but the UI matched nothing ([flow.py:365](src/fic/flow.py#L365)); and
detection of Fakturama's own native duplicate popup, which fires on Street tab-out as
well as at Save.

**"What if I run it twice on the same image?"**
`find_orders_by_reference` checks the saved documents for that Cust.Ref **before the
session is even opened**, and refuses with exit code 6, naming the existing document.
`--force` overrides.

**"Why read the database if the UI is the source of truth?"**
Only to choose *what to type into the search box*. Fakturama's search does
single-column substring matching, so a multi-word name split across FIRSTNAME/NAME
can't be found by any query — the resolver supplies a single reliable token (the last
name). The selection and its proof stay in the UI (§2.13).

**"What's not finished?"**
`grid.py`'s canvas fallback (`NotImplementedError` — not needed here, the grid is
accessible); S2/S3/S4 grounding strategies; per-tab UIA scoping
([session.editor_tab](src/fic/uia/session.py#L166), which degrades to a *safe* refusal,
not a wrong guess); the differing-delivery-address branch is implemented but not
live-verified; and there are no annotated screenshots in the repo. The README's
"What I skipped" section is the full list.

**"What was the hardest bug?"**
Two good answers. The **`2E+1`** one for subtlety: `str(Decimal("20").normalize())`
is scientific notation, Fakturama parsed it as 0, and a tax rate *named* "VAT 20%"
was stored as 0% — every subsequent lookup matched it happily, and it surfaced three
steps downstream as a line showing "0 %". The **combo read-back** one for what it
says about method: the verification rule this project is built on was itself
returning a false pass, and it was only caught by checking the *saved record* rather
than the field.
