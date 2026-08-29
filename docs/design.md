# Fakturama Image-to-Cash Automation — Design Document

*Part 1 deliverable. ~3 pages, no code.*

## 1. Overview

The system is a pipeline with five stages, mirroring the task's own structure: **extract
→ resolve/create Debtor → resolve/create Product (per line) → save Order → create and
verify linked Invoice**. Every stage follows the same rule: *act, wait for the UI to
settle, read back what actually persisted, compare it to the extracted source of truth,
and only then advance.* Nothing advances on an assumption. On any genuine ambiguity —
data that doesn't cleanly resolve to one exact answer — the run halts rather than
guesses, because every downstream value is money.

## 2. Control-discovery / grounding strategy

Fakturama is a desktop app (SWT/Eclipse RCP), so the approach is UIA-first.

The natural first choice is to bind controls to stable identifiers — `AutomationId`
above visible text, since text matching breaks the moment UI wording or language
changes. **That option isn't available here.** This build exposes no usable
`AutomationId`: probing the live tree returns empty strings, and the small add/delete
buttons above each list view expose no name, no automation id, no help text and no MSAA
description at all. There is nothing stable to bind to, so the strategy has to earn its
safety somewhere other than identifier quality.

It earns it by refusing:

- **Controls are resolved relative to their visible label** — the nearest control of an
  acceptable type in the expected direction. Where two candidates are near-equally
  close, the resolver **raises rather than picks**. That refusal, not the identifier, is
  what makes text-based resolution acceptable for financial data. Where one label
  legitimately appears twice (the Order editor carries a "VAT" label in its header *and*
  in its totals block), the call site states which one it means.
- **Column positions are read from each grid's own headers, never hardcoded.** A
  hardcoded column list is a guess that rots silently — an off-by-one column mapping
  produces a comparison against the wrong cell, which fails as "no match found" rather
  than as an error, and quietly creates duplicate records. Asking the grid what its
  columns are removes the guess.
- **No hardcoded coordinates.** Every click target is computed from the live UIA tree at
  the moment of the click.
- **Waiting is state-based, never a fixed sleep.** "Wait for the list to stabilize" means
  polling until row count and contents are unchanged across consecutive polls. The same
  pattern governs every dialog open, editor load and Save.
- A **vision/OCR fallback** scoped to a control's live bounding rectangle is the designed
  next tier for anything UIA cannot reach. It is not built: the one control class that
  genuinely resisted resolution — the unnamed list-toolbar buttons — was solved instead
  by driving the menu bar, whose items are properly named. The fallback stays on the
  roadmap rather than in the system.

**Read-back after every write** is the load-bearing guard. After typing into a grid cell
or clicking Save, the automation re-reads and compares. This catches a UI silently
rejecting or coercing an edit before it can reach a saved total.

One qualification matters more than the rule itself: **a read-back pointed at the wrong
object can only ever confirm your own mistake.** A dropdown whose *displayed text* is
re-read after writing will verify happily while the underlying selection never changed —
saving, for instance, a product with a 0% tax rate under a correct-looking name. A
readiness check aimed at a splash window will wait forever for a control that was never
going to appear there. Verification has to read the thing that persists, not the thing
that was typed, and this design treats that as the difference between a check and a
ritual.

## 3. Image-extraction strategy

Extraction is LLM-led rather than a classic OCR-engine pipeline: a multimodal LLM reads
the order image directly and returns a strict, schema-validated JSON object. A separate
OCR engine is not on the critical path.

1. **Primary extraction**: the image goes to the model with a fixed prompt and a JSON
   schema it must conform to. This handles template variance — different layouts, scanned
   versus photographed, minor skew — far better than a fixed OCR-regex pipeline.
2. **Provider-agnostic interface**: the call sits behind a thin interface
   (`extract(image) -> OrderJSON`), so the backing model is swappable by configuration
   rather than by code change. Gemini is the default and Claude the alternate, but the
   more important property is that neither is load-bearing. Model *availability* turns
   out to dominate model *choice* in practice: pinned model names get retired, pro-tier
   models exhaust free quota within a single image, and shared models return 503 under
   load. Extraction is therefore written to survive provider weather — bounded retry with
   backoff, an explicit per-request timeout, and model selection in configuration.
3. **Two-tier validation before any UI is touched.** Tier 1 is schema and type validation
   (blank SKU, non-positive quantity, PAID with no payment date). Tier 2 is the
   cross-field arithmetic types cannot express: per-line
   `qty × unit_net × (1 − discount)`, line totals against the order net, per-line VAT
   against the VAT total, and `net + VAT = gross` — each within a small tolerance, since
   three independently rounded values are being compared. **Nothing is ever
   auto-corrected**; a plausible-looking wrong number written into a financial record is
   the worst available outcome, so a disagreement stops the run instead.
4. **Completeness checking.** An extraction can be structurally valid and still have
   quietly dropped a field that matters — a debtor's contact name, say, while still
   returning that person's email address. That output passes every schema check and then
   causes the automation to fail to recognise an existing customer. The pipeline
   therefore separates fields whose absence *changes what the automation does* (those
   feeding the debtor match) from merely informational ones, re-reads the image when the
   former are missing, and prefers the more complete reading — but **only if both
   readings agree on every money-bearing field**. If they disagree, that is a halt: a
   document two readings interpret differently is one a human should settle, not one
   where the automation picks a winner.
5. **Self-consistency across providers** is available for money-bearing fields: both
   models read the image and the money fields are diffed, with disagreement halting the
   run. It is opt-in rather than default, since it doubles cost and latency on every run.
6. **Deterministic normalization happens in code, not in the LLM.** The model's job is to
   *structure* the page; decimal separators, dates and totals are normalized and
   independently recomputed afterward, then checked against both the document's own
   printed totals and what Fakturama ultimately saves.

## 4. Handling ambiguity and missing data

- **The Order's own selector dialogs are the existence check.** A debtor or product
  counts as existing only when it is found *and selected* through Fakturama's own UI, and
  a save is confirmed by re-selecting the record rather than by inspecting storage.
- **The database is read, deliberately, in a strictly bounded role.** Fakturama's search
  box matches a single string against one column at a time with no cross-column AND, so
  searching by a full company name routinely returns nothing for a customer that plainly
  exists. Reading Fakturama's own database file decides **what to type into the search
  box**, and supports two safety checks that cannot be made any other way: detecting that
  Fakturama has proposed a document number it has already used (otherwise discovered only
  after the entire order has been built and saved), and detecting that a customer already
  exists when the UI's exact-match test says otherwise. The UI remains the authority on
  selection; the database chooses a query and refuses unsafe actions.
- **Two independent checks disagreeing is itself a signal.** If the database says a
  company already exists but the UI's five-field match rejects every row, the run halts
  rather than create a second customer. A duplicate customer splits a real customer's
  invoice history across two records, and no later run can detect or undo it. The failure
  mode worth designing against here is not a broken check — it is two working checks that
  never compare answers.
- **Exact match is defined precisely**: case/diacritic/whitespace-normalized comparison
  against Company/First Name/Last Name/ZIP/City for a Debtor, exact SKU for a Product.
  "Ambiguous" therefore means *more than one row passes that test*, not merely that the
  search returned more than one row — read the other way, any common company name would
  incorrectly halt.
- **Delivery address differing from billing** is handled. This build has no per-address
  "role" concept: unchecking *"Delivery Address equals Invoice Address"* reveals a
  mirrored second column carrying an identical set of labels, distinguishable only by
  position — so every delivery write pins its target explicitly rather than trusting the
  label.
- A payment method with no defined payment-code mapping, a payment status other than
  PAID/UNPAID, or a PAID order missing a payment date all route to manual review rather
  than a best guess.
- A Product found by exact SKU is reused as a legitimate match, since SKU is the identity
  key. Logging a discrepancy when its stored Name or Description differs from the order —
  reuse, but record the drift — is designed and not built.
- **"Stop for manual review" halts the entire run**, not just the current line item, since
  the order's totals depend on every line resolving correctly.
- **Idempotency is enforced before the UI is opened.** Re-running the tool on the same
  order image must not quietly produce a second Order — a clean run that bills a customer
  twice is worse than a visible failure, because nothing downstream flags it. The saved
  documents' customer-reference column is matched against the extracted External
  Reference, and a match refuses the run and names the documents already on file. The
  comparison is on the parsed column rather than a search of the stored text, since a
  reference string can also occur inside an address or a note and refusing for the wrong
  reason is its own defect. An explicit override exists for the case where a second
  document is genuinely intended.

## 5. Tradeoffs

- **LLM-led extraction vs. classic OCR+regex.** OCR/regex is fast, cheap and
  deterministic but brittle across template variance. A vision LLM handles that variance
  well but is non-deterministic, adds latency and cost per run, and sends customer PII —
  name, address, contact details — to a cloud model. That is disclosed rather than
  glossed over; a fully local OCR pipeline would avoid it entirely at some cost to layout
  robustness. Independent numeric verification in code is what keeps the model's
  non-determinism out of the saved records.
- **Model choice is a configuration decision, not an architectural one.** Structured-
  output quality is a real differentiator between providers, but availability dominates
  it in practice. The design's actual requirement is that provider and model be swappable
  without code changes, and that transient failure be absorbed rather than surfaced as a
  crash. Model output is an input, and inputs vary; the surrounding code has to stay
  correct and diagnosable when they do.
- **Halt-on-ambiguity vs. best-effort completion.** Every ambiguous or missing-data case
  resolves to stopping. A best-effort mode would complete more orders end to end, but the
  cost of a wrong guess is an incorrect invoice or payment status — the conservative
  default is the right one for this domain.
- **Read-back verification after every write is expensive but non-negotiable** — and, per
  §2, only meaningful when it reads what actually persists rather than what was typed.
- **UIA-first vs. a fully vision-driven agent.** A fully vision-driven approach
  (screenshot plus model-chosen click targets) would be more resilient to UIA gaps but
  slower, less precise, and much harder to make deterministic for something as exact as a
  payment-code dropdown. UIA is preferred wherever the tree exposes what is needed.
- **The assumptions most likely to be wrong are the ones about someone else's UI.** This
  design assumed identifier quality the application does not provide, and model
  availability that did not hold. Both were survivable because the rules around them held:
  refuse when ambiguous, verify against what persists, never auto-correct money. An
  automation design of this kind should expect its assumptions about the target
  application to be wrong, and is best judged on how loudly it fails when they are.
