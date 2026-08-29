# Fakturama Image-to-Cash — Build Plan

What gets built, in what order, and why that order. Companion to `SPEC.md` (the *what*
and *how* of each piece) — this file is the *sequence and dependencies* between pieces:
how the work divides into workstreams, what blocks what, and what a workstream being
"done" actually means before moving to the next one.

---

## 1. The work divides into 7 workstreams

Not by clock time first — by **dependency**. Each row only starts once its "depends on"
column is done-per-§7's acceptance bar, not just "started."

| # | Workstream | Files (see `SPEC.md` §2) | Depends on | Produces |
|---|---|---|---|---|
| W1 | **Foundation** | `pyproject.toml`, `config/`, `models/`, `cli.py`, `errors.py`, `logging_setup.py` | nothing | A running `fic --help` that does nothing yet, but the domain model + exception hierarchy + CLI shell all typecheck |
| W2 | **Extraction** | `extraction/` incl. `providers/claude.py`, `providers/gemini.py`, `self_consistency.py`, `normalize.py`, `reconcile.py` | W1 (models, errors) | `fic extract order.png -o out.json` — a fully working, independently-testable subsystem. No UIA involved at all |
| W3 | **Grounding engine** | `uia/session.py`, `snapshot.py`, `locator.py`, `anchors.py`, `waits.py`, `actions.py` | W1 (errors); reads Fakturama live, doesn't need W2 | `fic probe` works; `resolve_by_label()` and the verified `set_text`/`select_combo`/`save` primitives exist and are contract-tested against recorded snapshots |
| W4 | **Item grid strategy** | `uia/grid.py` | W3 | The canvas-vs-accessible probe (`SPEC.md` §7) resolved for the real app, with a working read-back path either way |
| W5 | **Flow orchestration** | `flows/*.py`, `orchestrator.py` | W2 + W3 (+ W4 before Step 3 specifically) | Steps 1–5 of the brief, each independently runnable and verified, wired into the state machine |
| W6 | **Verification & reporting** | `report/trace.py`, `report/render.py`, `uia/capture.py` | threaded through W3–W5, not a separate phase | `runs/<id>/report.md` + screenshots — this is deliverable D4, built continuously, not staged at the end |
| W7 | **Tests** | `tests/unit`, `tests/contract`, `tests/e2e` | unit tests land alongside W2; contract tests alongside W3; e2e only once W5 is real | The test layers in `03-Stack-Validation-Testing-Errors.md` §5 |

**W2 and W3 are independent of each other and can be built in either order or in
parallel** — extraction never touches Fakturama, the grounding engine never touches an
order image. This is deliberate: it means a stall in one (say, Fakturama's item grid
turning out to be a canvas widget that fights back, W4) doesn't block proving out the
other. W5 is the only workstream that needs both finished.

---

## 2. Build order (dependency graph)

```
W1 Foundation
 ├──▶ W2 Extraction ─────────────┐
 └──▶ W3 Grounding engine        │
        └──▶ W4 Item grid        │
               └──▶ W5 Flow orchestration ◀──┘
                      │  (Steps 1 → 2 → 3 → 4 → 5, strictly in this order —
                      │   each step's exit condition is the next step's entry condition)
                      ▼
                   W6 Verification/reporting  (built INTO W3–W5, not after)
                   W7 Tests                    (built ALONGSIDE each workstream, not after)
```

Two things this graph is trying to prevent:

- **Building W5 before W2/W3 are solid.** Wiring the state machine against a flaky
  extractor or an unverified locator just relocates the bugs into the hardest-to-debug
  layer. W2 and W3 each get their own working CLI entry point (`fic extract`, `fic
  probe`) specifically so they're provably correct in isolation first.
- **Treating W6/W7 as end-of-timebox cleanup.** A report renderer written after the run
  is done can only describe what was logged; if trace/screenshot capture wasn't wired
  into W3–W5 from the start, D4 becomes a scramble. Same for tests — a unit test for
  `reconcile.py` written the same hour `reconcile.py` is written costs minutes; written
  three hours later after the API has drifted, it costs much more.

---

## 3. Time allocation

This is `SPEC.md` §13's timebox table, restated against the workstreams above so the
two views stay consistent with each other:

| Slot | Workstream(s) | Exit bar before moving on |
|---|---|---|
| 0:00–0:30 | W1 | `fic --help` runs; models + errors import cleanly; CLI shell in place |
| 0:30–1:15 | W2 | `fic extract data/input/order_001.png` reproduces the golden fixture exactly (§3's canary: `297.50` gross price) |
| 1:15–2:15 | W3 (S0/S1 only; defer S2/S3/S4) | `fic probe` prints the live tree; `resolve_by_label` finds every Step-1 field with a passing contract test |
| 2:15–3:00 | W4 + W5 Step 1–2 (select-path only) | An existing Debtor can be found and selected from a live New Order, verified by read-back |
| 3:00–3:45 | W5 Step 2 (creation branch + payment-method sub-branch) | A brand-new Debtor + payment method can be created and reselected into the Order |
| 3:45–4:30 | W5 Step 3 | One item line resolved end-to-end (select-or-create Product, VAT, line completion) |
| 4:30–4:50 | W5 Step 4–5 | Order saved + verified in Documents; Invoice created via the follow-up action, payment state applied |
| 4:50–5:00 | W6 finalize + D1–D6 gate | See §4 — this slot is protected, never sacrificed for one more flow branch |

W6 and W7 don't get their own slots because they're not phases — every slot above
implicitly includes "and the trace/screenshot for this step gets written" and "and the
unit/contract test for this piece gets written," per §2's second bullet.

---

## 4. Milestone → deliverable mapping

| Deliverable | Satisfied by | Checked at |
|---|---|---|
| D1 design doc | Written separately, ≤4 pages, before any of W1–W7 starts (`SPEC.md` §13.0) | Part 1, own 90-minute clock |
| D2 source code, clear structure | W1's layout, populated incrementally by W2–W5 | continuous; `git log` should show one commit roughly per workstream slot, not one dump at 5:00 |
| D3 setup instructions | `README.md`, written from `03-Stack-Validation-Testing-Errors.md` §1 (`uv` commands) | drafted during W1, finished in the 4:50–5:00 slot |
| D4 screenshots/recording | W6, captured live during W3–W5 | curated (not created) in the 4:50–5:00 slot |
| D5 README + "what I skipped" | Written continuously — every workstream that gets cut (see §5) gets its line the moment it's cut, not reconstructed from memory later | finished in the 4:50–5:00 slot |
| D6 "3 more hours" answer | Already drafted in `01-Full-Engineering-Spec.md` §7 | copied into README, revised if reality diverged from the plan |

---

## 5. If time runs short — cut in this order

Cutting means: implement the select-path, document the gap, don't attempt the branch.
Never means: skip verification on the parts that *are* built.

1. **VAT creation branch** (W5 Step 3) — Fakturama ships common VAT rates by default,
   so the select-path likely covers the golden fixture; the create-branch is the
   first thing to leave as "designed, not implemented."
2. **OCR fallback extractor** (W2) — Claude/Gemini vision is the primary path anyway;
   OCR was always the secondary signal, not load-bearing (per `03-...md` §2's provider
   design).
3. **`--resume` / mid-run recovery** (W1/W5) — nice for robustness, not needed to
   demonstrate the flow once, end to end.
4. **The DB read-only oracle** (W7 e2e) — test-only convenience, explicitly forbidden
   as a substitute for UI verification (R2) even where it exists.

**Never cut, regardless of time pressure:** read-back verification after writes (this
is the entire point of the design), the golden-fixture extraction test, and the D5
README "what I skipped" section — an honest gap list is worth more than a silently
incomplete flow.

---

## 6. "Done" per workstream — the acceptance bar before moving to the next

| Workstream | Not done until |
|---|---|
| W1 | `mypy --strict` and `ruff check` pass on the skeleton; every error class in `03-...md` §4 exists and is importable |
| W2 | The golden fixture (§3's `WEB-2026-0714-A17` order) round-trips through `extract()` and matches `data/golden/order_001.json` field-for-field, including the `297.50`/`47.60` gross-price canary |
| W3 | `fic probe` output is legible enough to write a `selectors.yaml` entry from it *without* looking at the running app a second time; every S1 resolution has a passing contract test, including the ambiguity-refusal case |
| W4 | The grid strategy that's actually needed (canvas or accessible — determined by probing the real app, not assumed) has one working read-back path, proven on one real line item |
| W5 | Each step (1–5) independently passes its own read-back verification against a live Fakturama instance before the next step is wired in — not "looks right," a passing assertion |
| W6 | `runs/<id>/report.md` for a completed run has one annotated screenshot per flow stage, generated from the actual trace, not hand-assembled afterward |
| W7 | `uv run pytest` (unit + contract) is green in CI-safe mode; `-m e2e` has been run at least once against a real Fakturama and passed |
