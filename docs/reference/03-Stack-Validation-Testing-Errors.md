# Fakturama Image-to-Cash — Tooling, Validation, Testing, Errors

Companion to `SPEC.md` (architecture/flow/repo layout) and `01-Full-Engineering-Spec.md`
(corner-case catalog). This file answers one question precisely: **what do I actually
run, install, and write to make validation/tests/errors real, not aspirational.**

One reconciliation up front: `SPEC.md` §4.1 names Claude as the only extraction
provider. `02-Design-Doc-Submission.md` commits to Claude-primary + **Gemini as
fallback/cross-check** (for the self-consistency check on money fields, X8 in the
corner-case catalog). This doc builds on the newer decision — `google-genai` is added
to dependencies and `extraction/providers/` is added to the repo layout below.

---

## 1. Tooling: `uv`

**Use `uv` for everything — Python version, venv, dependency resolution, lockfile,
running the CLI.** Not `pip install`, not a hand-rolled `venv`.

Why, concretely for this project: the dev box is Linux and the target is Windows-only
(UIA is a Win32 API) — `uv`'s lockfile (`uv.lock`) plus `uv python pin` make the
Windows environment reproducible from a clean clone in one command, which matters a lot
for deliverable D3 ("a reader with a clean Windows box can go from zero to `fic run`").
`uv sync` is also fast enough that CI can afford to run the unit + contract test layers
(§4 of `SPEC.md`) on every commit without the Windows-only e2e layer slowing anything down.

```bash
# one-time, on the Windows box
uv python pin 3.12
uv init --package fakturama-image-to-cash      # or: uv init, if the repo already exists
uv add anthropic google-genai pydantic pywinauto uiautomation comtypes \
       pillow mss pytesseract typer rich structlog pyyaml python-dateutil
uv add --dev pytest pytest-mock pytest-cov ruff mypy

# day to day
uv sync                          # installs exactly what uv.lock pins — commit the lock file
uv run fic run data/input/order_001.png
uv run fic probe
uv run pytest                    # unit + contract layers (Linux-safe, see §4)
uv run pytest -m e2e             # Windows only, against a live Fakturama
uv run ruff check .
uv run mypy src

# reproducing on a fresh Windows machine
git clone <repo> && cd fakturama-image-to-cash
uv sync                          # venv + exact pinned deps, no manual pip juggling
```

`uv.lock` is committed. `pyproject.toml` is the source of truth for version *ranges*;
the lock file is the source of truth for what actually gets installed — this is what
makes "works on my machine" not the failure mode when this gets evaluated on someone
else's Windows box.

### `pyproject.toml`

```toml
[project]
name = "fakturama-image-to-cash"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=1.0",        # vision extraction — primary provider
    "google-genai>=0.3",     # vision extraction — fallback + cross-check provider
    "pydantic>=2.7",         # schema validation (tier 1, see §3)
    "pywinauto>=0.6.8",      # UIA backend, high-level control wrappers
    "uiautomation>=2.0.20",  # raw UIA tree walking where pywinauto is too opinionated
    "comtypes>=1.4",
    "pillow>=10",
    "mss>=9",                # fast window screenshots for the run report + vision fallback
    "pytesseract>=0.3.10",   # OCR fallback extractor + grid read-back-by-picture
    "typer>=0.12",           # CLI
    "rich>=13",              # console run log
    "structlog>=24",         # JSONL trace (run report)
    "pyyaml>=6",             # selectors.yaml, config/app.yaml
    "python-dateutil>=2.9",
]

[dependency-groups]
dev = ["pytest>=8", "pytest-mock>=3.14", "pytest-cov>=5", "ruff>=0.6", "mypy>=1.11"]

[project.scripts]
fic = "fic.cli:app"

[tool.pytest.ini_options]
markers = [
    "e2e: full flow against a live Fakturama instance (Windows only, not in CI)",
    "live: hits a real extraction provider API instead of a mocked response",
]
addopts = "-m 'not e2e and not live' --cov=fic --cov-report=term-missing"

[tool.ruff]
line-length = 100
[tool.mypy]
strict = true
```

`addopts` defaults `pytest` to the CI-safe subset; `uv run pytest -m e2e` or
`-m live` opts back in explicitly. Nobody accidentally fires a paid API call or drives a
real Fakturama window by running `pytest` with no arguments.

---

## 2. Repo layout — delta from `SPEC.md` §2

Everything in `SPEC.md`'s tree stands. Two additions for the Claude+Gemini decision:

```
src/fic/extraction/
├── base.py                  # Extractor protocol: extract(image_path) -> SourceOrder
├── providers/
│   ├── claude.py            # primary — SPEC.md §4.1, unchanged
│   └── gemini.py            # fallback + cross-check, same output contract
├── self_consistency.py      # calls both providers on money-bearing fields, diffs,
│                             # raises ManualReviewRequired on disagreement (X8)
├── ocr.py                   # unchanged
├── normalize.py             # unchanged
└── reconcile.py             # unchanged — see §3 below, this is tier-2 validation
```

`base.py`'s `Extractor` protocol is what makes `providers/claude.py` and
`providers/gemini.py` interchangeable — both return a validated `SourceOrder`, so
`self_consistency.py` doesn't care which one is "primary" beyond config, and a provider
outage (X7 in the corner-case catalog) is a one-line config change, not a code change.

---

## 3. Validation — two tiers, neither optional

**Tier 1 — schema validation, at the extraction boundary (`pydantic`).** Runs the
instant a provider returns JSON, before `reconcile.py` ever sees it. This is where
several corner cases become *impossible to construct*, not just checked later:

```python
# src/fic/models/source.py — additions to SPEC.md §3's models
from decimal import Decimal, ROUND_HALF_UP
from pydantic import BaseModel, field_validator, model_validator

def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class SourceItem(BaseModel):
    sku: str
    quantity: Decimal
    unit_net_price: Decimal
    discount_pct: Decimal = Decimal("0")
    vat_pct: Decimal
    line_net_total: Decimal

    @field_validator("sku")
    @classmethod
    def sku_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sku must not be blank — E5, hard stop before Fakturama opens")
        return v

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError(f"quantity must be > 0, got {v} — E7")
        return v

    @field_validator("discount_pct")
    @classmethod
    def discount_in_range(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v <= Decimal("100")):
            raise ValueError(f"discount_pct out of [0,100]: {v}")
        return v

class SourcePayment(BaseModel):
    method: str
    paid_status: str
    payment_date: "date | None" = None

    @field_validator("paid_status")
    @classmethod
    def known_status_only(cls, v: str) -> str:
        # Brief only defines PAID/UNPAID (§5.3). Anything else (PARTIALLY PAID,
        # OVERDUE, REFUNDED) is E8 — reject here rather than let it drift into
        # a branch that guesses which side of the paid/unpaid line it's "closer to".
        if v not in ("PAID", "UNPAID"):
            raise ValueError(f"unrecognized payment status {v!r} — E8, not PAID/UNPAID")
        return v

    @model_validator(mode="after")
    def paid_requires_date(self):
        # E9: the brief forbids inventing a date but requires one when PAID.
        # Reject at the model boundary rather than resolve the contradiction downstream.
        if self.paid_status == "PAID" and self.payment_date is None:
            raise ValueError("PAID status with no payment_date — E9, contradictory input")
        return self
```

A `pydantic.ValidationError` raised here is caught once, at the extraction call site,
and re-raised as `ExtractionError` (§5) with exit code 5 — before any UIA call happens.

**Tier 2 — business-rule / cross-field validation, after extraction succeeds
(`reconcile.py`).** Things a single field's type can't express: line-vs-total
arithmetic, tolerance-based float comparison, confidence thresholds. This is
`SPEC.md` §4.4's seven rules, unchanged — tier 1 doesn't replace it, tier 1 just moves
the cases that *can* be caught structurally (E7, E8, E9) out of arithmetic checking and
into the type system, so `reconcile.py` stays focused on the checks that genuinely need
cross-field arithmetic (the four O1–O4 rules in the corner-case catalog: rounding
tolerance, line-vs-total sum, VAT sum, and the pre-flight duplicate-run check).

**What deliberately does *not* get a validator:** company name format, address
completeness beyond required fields, alias presence. These are the E13–E16 corner
cases that need a documented *policy* (normalize-for-comparison-only, derive-a-slug,
leave-blank) rather than a reject/accept gate — see `01-Full-Engineering-Spec.md` §4.1
and §5 for the reasoning. A validator that rejected an order for a missing Alias would
be wrong, not safe.

---

## 4. Exceptions — full hierarchy

`SPEC.md` §10/§11 names the exception types and exit codes but not the hierarchy.
Concretely:

```python
# src/fic/errors.py
from __future__ import annotations
from pathlib import Path
from typing import Any

class AutomationError(Exception):
    """Base for everything the flow can raise. Always carries evidence for the run
    report — never just a message, because the operator reading runs/<id>/report.md
    needs to know *why*, not just *that*."""
    exit_code: int = 1

    def __init__(self, message: str, **evidence: Any):
        super().__init__(message)
        self.message = message
        self.evidence = evidence

class ExtractionError(AutomationError):
    """Extraction or tier-1/tier-2 validation failed before any UI interaction.
    Nothing in Fakturama has been touched — safe to just fix the input and re-run."""
    exit_code = 5

class ManualReviewRequired(AutomationError):
    """A business rule says stop — not a bug, a designed terminal outcome (R5).
    Covers: ambiguous debtor/product/VAT/payment-method matches, unmapped payment
    codes, unrecognized payment status, cross-provider extraction disagreement (X8)."""
    exit_code = 2

    def __init__(self, reason: str, *, candidates: list | None = None,
                 screenshot: Path | None = None, **evidence: Any):
        super().__init__(
            reason,
            candidates=candidates or [],
            screenshot=str(screenshot) if screenshot else None,
            **evidence,
        )

class AmbiguousControl(ManualReviewRequired):
    """The grounding engine found ≥2 candidates within AMBIGUITY_MARGIN_PX of each
    other (SPEC.md §5 S1). Refusing to guess between them is the point, not a bug."""

class OptionUnavailable(ManualReviewRequired):
    """An exact dropdown/combo value was required and genuinely absent — e.g. a
    payment method with no entry in the payment-code map (E10)."""

class VerificationFailed(AutomationError):
    """A write did not read back as intended, or Save did not clear the dirty flag.
    This is the exception U7/U9/U10 exist to catch before it reaches a saved record."""
    exit_code = 3

class ControlNotFound(AutomationError):
    """The grounding engine's full S0-S4 cascade could not resolve a required
    control. Distinct from AmbiguousControl: this is zero candidates, not many."""
    exit_code = 4

class AlreadyProcessedError(AutomationError):
    """Pre-flight (O4) found an existing Order with this External Reference. The run
    is a safe no-op unless invoked with --force."""
    exit_code = 6
```

Exit code table (extends `SPEC.md` §10 with `AlreadyProcessedError`):

| Code | Exception | Meaning |
|---|---|---|
| 0 | — | Done, verified |
| 2 | `ManualReviewRequired` (+ subclasses) | Ambiguous or unresolvable business data — expected, first-class outcome |
| 3 | `VerificationFailed` | A write didn't persist as intended |
| 4 | `ControlNotFound` | Grounding engine exhausted its cascade |
| 5 | `ExtractionError` | Bad/contradictory source data, caught before touching the UI |
| 6 | `AlreadyProcessedError` | Idempotency guard — this order was already processed |

**Rule:** `ManualReviewRequired` and its subclasses are *never* caught and retried —
per `SPEC.md` §11, "an ambiguous debtor is ambiguous the second time too." Only
`ControlNotFound`/timeout-shaped failures are eligible for the bounded retry inside
`wait_until` (transient UI slowness) — business-rule halts are not.

---

## 5. Testing — concrete cases, mapped to the corner-case catalog

`SPEC.md` §12 gives the four-layer matrix (unit / unit-extractor / contract / e2e).
Here's what actually goes in each file, named so a reviewer can match a test to the
corner case it proves is handled — this is the difference between "we thought about
X8" and "there's a red test if X8 regresses."

```
tests/unit/test_source_validation.py       # tier 1, §3 above
    test_paid_status_without_payment_date_rejected        # E9
    test_unrecognized_payment_status_rejected              # E8
    test_zero_quantity_rejected                             # E7 (qty)
    test_zero_price_line_accepted                           # E7 (price — valid input)
    test_blank_sku_rejected                                 # E5
    test_discount_pct_out_of_range_rejected

tests/unit/test_reconcile.py                # tier 2, SPEC.md §4.4
    test_line_net_matches_within_tolerance                  # O1
    test_line_net_mismatch_beyond_tolerance_raises           # O2
    test_order_total_reconciles_from_lines
    test_gross_price_formula_matches_golden_297_50           # canary, see below
    test_vat_summed_per_line_not_on_order_total

tests/unit/test_normalize.py
    test_german_decimal_separator_1234_56
    test_country_deutschland_maps_to_germany                 # D6
    test_country_unmapped_raises                             # D6
    test_date_genuinely_ambiguous_flagged_not_guessed         # E3

tests/unit/test_golden_extraction.py         # mocked provider response
    test_claude_extract_reproduces_order_001_golden
    test_gemini_extract_reproduces_order_001_golden
    test_product_master_gross_price_297_50_and_47_60          # the canary from
                                                                # 01-Full-Engineering-Spec.md

tests/unit/test_self_consistency.py
    test_agreement_between_providers_passes_through
    test_disagreement_on_money_field_raises_manual_review      # X8

tests/contract/test_locator_debtor_dialog.py    # recorded UIA snapshots, no Fakturama needed
    test_single_exact_match_selected                          # D3
    test_zero_exact_matches_routes_to_creation_branch          # D2
    test_multiple_exact_matches_raises_manual_review           # D4
    test_partial_match_company_only_is_not_exact               # D2 (the "conflicting ≠
                                                                # raw search returned >1" distinction)
    test_ambiguity_margin_refuses_near_tie_label_anchors

tests/contract/test_locator_product_dialog.py
    test_sku_exact_match_ignores_description_drift              # P1
    test_vat_reuse_requires_name_value_and_code_all_match        # P2
    test_new_sku_created_then_found_on_second_occurrence          # P5

tests/contract/test_selectors_config.py
    test_every_selectors_yaml_entry_resolves_against_fixture_tree
    test_never_write_fields_refuse_a_write_attempt                # Standard VAT, Set as standard
    test_readonly_intent_fields_refuse_a_write_attempt             # No., Customer ID, Invoice No.

tests/e2e/test_full_flow_golden_order.py     # Windows only, live Fakturama, reset workspace
    test_order_001_end_to_end_saved_and_verified
    test_rerun_same_order_is_noop_not_duplicate                    # O4 / D7
    test_delivery_address_differs_creates_second_address           # E15 — the brief's
                                                                     # unstated else-branch
```

**Coverage target:** tier-1/tier-2 validation and `reconcile.py` at ~100% branch
coverage — these are pure functions handling money, cheap to fully cover, and the
highest-consequence code in the repo if wrong. The locator engine (`contract/`) is
covered via recorded snapshots rather than a percentage target, since its risk is
"resolves the wrong control," which line coverage doesn't measure — the ambiguity-
refusal and never-write tests matter more than a coverage number there.

**CI (Linux-capable):** `uv run pytest` runs unit + contract only (see `addopts` in §1)
— no Fakturama, no Windows, no live API calls. `-m e2e` and `-m live` are explicit
opt-ins, run manually on the Windows dev box per `SPEC.md` §12's "Windows only,
manual/nightly."

---

## 6. What this file deliberately leaves to `SPEC.md`

Repo layout (beyond the two additions in §2), the grounding-engine cascade, the flow
state machine, `selectors.yaml`, the CLI surface, and the timebox plan all stand as
written in `SPEC.md` — no need to restate them here. This file exists to make three
things concrete that `SPEC.md` named but didn't fully specify: the exact tool (`uv`)
and command surface for setup/CI, the tier-1 validator code (not just "pydantic
models"), and the exception hierarchy with its full class tree (not just a name list).
