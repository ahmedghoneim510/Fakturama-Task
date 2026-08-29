# Fakturama Image-to-Cash Automation — Full Engineering Spec

**Status:** working document (not the submission). Use this to build the system and to
distill the 1–4 page Part 1 design doc. Verified against the actual assignment brief and
the sample order image (`WEB-2026-0714-A17`, Northstar Office GmbH) and Fakturama UI
screenshots included in the brief.

Cross-checked math from the sample image (confirms the brief's formulas are internally
consistent and gives us a clean baseline test fixture):
- Line 1: 2 × €250.00 × (1 − 10%) = **€450.00** net
- Line 2: 3 × €40.00 × (1 − 0%) = **€120.00** net
- Net total €570.00, VAT 19% = €108.30, Gross = **€678.30** ✓
- Product master gross price (3.9 formula): €250.00 × 1.19 = **€297.50** — matches the
  "Price (gross)" field shown in the New Product screenshot exactly. Good canary: if a
  reimplementation doesn't reproduce 297.50 for this fixture, the rounding/formula wiring
  is wrong.

---

## 1. Purpose

Turn one order image into a saved, verified Order + linked Invoice in Fakturama, using
the Order's own Debtor/Product selector dialogs as the *existence check* (never a
side-channel DB query), creating master data only on a genuine miss, and halting for
manual review on any ambiguity — never guessing on data that touches money.

## 2. Architecture

```
 Order Image
     │
     ▼
[1] Extraction Pipeline        LLM-native vision extraction → structured JSON,
     │                         schema-validated. Provider-agnostic interface:
     │                         Claude (primary) / Gemini (fallback + cross-check).
     │                         OCR is optional secondary signal only, not required.
     │
     ▼
[2] Orchestrator / State Machine   one state per brief section (Order → Debtor →
     │                             Product×N → Order totals → Save → Invoice → Verify)
     ▼
[3] Grounding / Control-Discovery Layer   UIA-first tree queries, exposes a small
     │                                    verb set: find(), click(), setText(),
     │                                    selectRow(), waitFor(), readBack()
     ▼
[4] Fakturama (SUT)
     │
     ▼
[5] Verification Layer   re-reads persisted state after every Save/select and
                          compares to the extracted source of truth
```

Each of [2]'s states is implemented as: **act → wait-for-idle → read back → compare to
expected → advance or halt**. No state advances on an assumption; it advances on a
confirmed read-back. This single rule is what makes most of the corner cases below
tractable — most of them are really "what does 'confirm' mean, mechanically."

## 3. Extraction data contract

The pipeline must emit one JSON object per order, schema-validated before anything
touches the UI. Minimum shape (brief §1.2, plus gaps identified in §7 below):

```jsonc
{
  "order": { "date": "YYYY-MM-DD", "external_reference": "string",
             "currency": "EUR", "discount_pct": 0, "shipping": "free|amount",
             "source_net_total": 0.00, "source_vat_total": 0.00, "source_gross_total": 0.00 },
  "debtor": { "company": "string", "contact_first_name": "string|null",
              "contact_last_name": "string|null", "alias": "string|null",
              "billing_address": {...}, "delivery_address": {...} | "same_as_billing",
              "email": "string|null", "phone": "string|null" },
  "payment": { "method": "string", "status": "PAID|UNPAID|OTHER",
               "payment_date": "YYYY-MM-DD|null" },
  "items": [ { "sku": "string", "description": "string", "qty": 0,
               "unit_net_price": 0.00, "vat_pct": 0.0, "discount_pct": 0.0,
               "source_line_total": 0.00 } ],
  "extraction_confidence": { "...": 0.0 }
}
```

Every money/date/percent field carries a per-field confidence score. Anything below
threshold is treated the same as "ambiguous" downstream — routed to manual review, not
silently accepted.

## 4. Corner-case catalog

Organized by the brief's own five stages, then cross-cutting issues.

### 4.1 Image extraction

| # | Case | Handling |
|---|---|---|
| E1 | Skewed/rotated/low-res/glare photo | Deskew + upscale pre-pass; if OCR confidence still low, flag whole doc for review rather than guess individual fields |
| E2 | Decimal/thousands separator ambiguity (1.234,56 vs 1,234.56) | Locale-aware parse driven by detected currency/language, not a hardcoded format |
| E3 | Date format ambiguity | Normalize to ISO; if the source is genuinely ambiguous (e.g. 03/04/26), flag rather than guess |
| E4 | OCR confusables in SKUs (O/0, l/1/I, S/5) | Downstream Product search is exact-match against the live catalog anyway, so a misread SKU naturally surfaces as "no exact row" → still routes to the right branch, but log the raw OCR token alongside the searched token for debuggability |
| E5 | Missing required field (no SKU, no VAT%, no external ref) | Hard stop before opening Fakturama at all — don't start a partial automation run |
| E6 | Multiple VAT rates in one order | Handled naturally — VAT lookup/creation is per line item, not per order |
| E7 | Zero-price / promo line, zero-qty line | Zero price is valid input, not an error; zero qty is not — flag |
| E8 | Payment status other than PAID/UNPAID (PARTIALLY PAID, OVERDUE, REFUNDED) | Brief only defines two behaviors (§5.3). Anything else is an **unrecognized status → stop for manual review**, not a guess at which branch it's "closer to" |
| E9 | PAID but no Payment Date present | Brief forbids inventing a date (§5.3) but also requires setting one when PAID. This is a genuine contradiction in the brief for this input — treat as **stop for manual review**, don't silently pick either horn |
| E10 | Payment method not in the 3-entry code map (Bank Transfer/Credit Card/SEPA) — e.g. "PayPal", "Cash" | No defined payment-code exists → stop for manual review rather than inventing a code |
| E11 | Order-level discount/shipping present in source | Brief's extraction list (§1.2) never actually asks for these fields, yet §4.2 assumes they might exist. Add them to the extraction schema explicitly (see §3) — this is a spec gap, not an edge case to special-case at runtime |
| E12 | Source header totals don't reconcile with recomputed line totals (bad source math) | Don't "fix" the source. Recompute independently; if it disagrees with the source total beyond tolerance (§4.6), stop — never silently trust one over the other |
| E13 | Company legal-form/diacritic variants (GmbH vs GmbH & Co. KG, Ö/ä/ß) | Normalize for the *exact-match comparison only* (trim, casefold, collapse whitespace) — never normalize the value that actually gets typed into Fakturama |
| E14 | Contact name is a single "full name" string, or absent entirely (company-only order) | Define one splitting heuristic up front and document it; if absent, leave First/Last blank rather than fabricating a name — flag as an explicit assumption in the README |
| E15 | Delivery address differs from billing (not just "identical" case) | Brief's §2.8 only specifies the identical case. Design must also create a second address with the Delivery role when they differ — call this out explicitly, it's not covered by the brief |
| E16 | Alias not present in source | Brief (§2.9) says "enter Alias name" with no fallback rule. Define a deterministic derivation (e.g. slug of company + city) rather than leaving it to whatever the LLM feels like that run |

### 4.2 UI automation / control discovery

| # | Case | Handling |
|---|---|---|
| U1 | Fakturama not running, minimized, or not focused at start | Explicit pre-flight: launch/attach, bring to foreground, confirm main window UIA root is reachable before anything else |
| U2 | Non-English/localized UI labels | Prefer `AutomationId`/`ControlType`/tree position over `Name` text where Fakturama exposes it (SWT apps are inconsistent here); detect UI language once at startup and fail fast with a clear error if labels can't be resolved, rather than silently misclicking |
| U3 | Editor tab/dialog not fully loaded (async population) | Never fixed `sleep()`. Poll: control exists → control enabled → value stable across N consecutive polls. Same pattern for every wait in the flow |
| U4 | Stale Order tab left open from a previous run | Identify the *current* tab by the just-created Order number, not "the active tab," before driving it |
| U5 | Select-address / Select-product dialog "list not stabilized yet" | Stabilization = row count and row contents unchanged across N polls with no visible loading indicator, not a fixed delay |
| U6 | OK/Cancel disabled until a row is selected | Wait for enabled state before invoking, don't just click-and-hope |
| U7 | Grid cell edit silently rejected (e.g. non-numeric typed into Qty) | After every cell write, **read the cell back** and compare to intended value before moving on — this is the single most important guard in the whole system, since a silently-rejected cell edit would otherwise propagate wrong totals all the way to a saved Invoice |
| U8 | In-progress field edit not committed before Save is clicked | Explicitly blur/Tab out of the last-edited control before invoking Save |
| U9 | Save triggers a validation error dialog | Detect dialog appearance, surface the message, halt — never treat "I clicked Save" as "it saved" |
| U10 | Transient timeout on Save — did it actually succeed? | Before any retry, re-check persisted state (does the Documents list already show the row?) to avoid double-saving/duplicating a document |
| U11 | Fakturama's own search misses an existing record due to trailing whitespace/casing that a human wouldn't notice | Re-search post-save and confirm a *single* row is returned — catches the case where "exact match" logic and Fakturama's own search index disagree |
| U12 | DPI scaling / multi-monitor, if any fallback visual click is ever used | Required approach is UIA-first; if OCR/vision fallback is used for a genuinely inaccessible control, resolve in logical/UIA coordinates, never hardcoded pixels (this is an explicit requirement in the brief, not just good practice) |
| U13 | App becomes unresponsive mid-flow | Bounded timeout, then abort with a full diagnostic bundle (screenshot + UIA tree dump + last known state) rather than hanging indefinitely |

### 4.3 Debtor resolution (brief §2)

| # | Case | Handling |
|---|---|---|
| D1 | Search returns 0 rows | → creation branch, per brief |
| D2 | Search returns rows, none of which pass the 5-field exact-match test (Company/First/Name/ZIP/City) | Treat as **0 exact matches**, not "conflicting" — go to creation branch. "Conflicting/ambiguous" (brief's wording) should mean *more than one row passes the exact-match test*, not *the raw search returned more than one row*. This distinction isn't spelled out in the brief and is worth stating explicitly since it changes behavior a lot |
| D3 | Exactly one row passes the exact-match test | Select it |
| D4 | More than one row passes the exact-match test (true duplicate debtor records) | Stop for manual review, per brief |
| D5 | Matching debtor found but its stored Payment Method differs from this order's extracted method | Not blocking — §5.2 re-applies the payment method on the *Invoice* from the image regardless of what's on the Debtor record. Document this explicitly so it isn't mistaken for a conflict |
| D6 | Country field: source says "Deutschland", dropdown expects "Germany" (or a code) | Needs an explicit mapping table; unmapped country → stop for review rather than leaving the field wrong |
| D7 | Re-running on the same image (idempotency) | Master-data creation is naturally idempotent *if* exact-match search works correctly (D2/D3 logic). A genuine duplicate run is only a risk under concurrent execution — document as a single-instance, sequential-run assumption |

### 4.4 Product resolution (brief §3)

| # | Case | Handling |
|---|---|---|
| P1 | Exact SKU found, but stored Name/Description differs from this order's description (product was renamed since master data was created) | Brief only requires SKU match (§3.3) — doesn't require Name/Description equality. **Decision needed and documented**: treat SKU as the sole identity key, log the discrepancy, proceed without overwriting the master record. (Flagged as a judgment call the brief leaves open — the alternative, silently overwriting Name/Description on every order, seems clearly worse) |
| P2 | Same VAT ambiguity pattern as D2/D4, but for VAT rows | Same rule: 0 exact matches → create; >1 exact matches → stop |
| P3 | Fractional VAT rates (7.5%, 5.5%) and their decimal-separator in the "VAT 7.5%" name string | Normalize the generated name string consistently (always `.` regardless of source locale) so repeat lookups match what was created |
| P4 | Gross price rounding (§3.9 formula) | Fix the rounding rule explicitly: round-half-up to 2dp. Confirmed against the sample fixture (250 × 1.19 = 297.50 exactly, no rounding ambiguity in that case — but pick and document the rule before hitting a case that needs it, e.g. 33.33 × 1.19) |
| P5 | Same new SKU appears on two lines in the same order | First occurrence creates Product+VAT; because each line re-runs the full search branch (§3.1), the second occurrence's search should now find it — this needs a test case, since it's easy to accidentally cache "not found" from the first lookup |
| P6 | Overlong description truncated by a DB field limit | Validate length pre-Save; if Fakturama silently truncates anyway, the post-save read-back (per U7's philosophy) will catch a mismatch |

### 4.5 Order totals & save (brief §4)

| # | Case | Handling |
|---|---|---|
| O1 | Recomputed line/order totals vs. source totals disagree by a cent or two (rounding) | Define an explicit tolerance (e.g. ±€0.01 per line, ±€0.02 order-level) rather than exact float equality — exact equality will spuriously fail on legitimate rounding |
| O2 | Disagreement beyond tolerance | Stop for manual review — never auto-adjust either the order or the "expected" value to force a match |
| O3 | Fakturama's own recalculated line Price differs slightly from the manually-computed value (§3.16) due to its internal rounding | Same tolerance-based comparison as O1, and document which value "wins" for downstream checks (Fakturama's persisted value, since that's what's actually saved) |
| O4 | Re-running against an order already fully processed (same External Reference) | Pre-flight check: search Documents by Cust.Ref before starting; if a matching Order+Invoice already exists and matches, treat the run as a no-op rather than creating a duplicate Order |

### 4.6 Invoice (brief §5)

| # | Case | Handling |
|---|---|---|
| I1 | Must use the Documents-list follow-up "Invoice" action, not the toolbar button | Explicit per brief (§4.6) — implement exactly this, including the window/tab-switch this implies (Order editor → Documents list → new Invoice tab) |
| I2 | Invoice's default Payment Method (inherited from Debtor) differs from the image's payment method for *this* order | Expected and common (e.g. debtor usually pays by Bank Transfer but this order was Credit Card) — §5.2 requires actively overwriting it every time, not just checking whether it already matches |
| I3 | Reopening the Invoice to verify (§5.6) accidentally leaves it dirty | Close without saving after a read-only verification pass |
| I4 | Required payment method missing from Fakturama entirely at Invoice stage | Brief says stop for manual review (§5.2) — note this can't happen if Debtor creation (§2.10) already ensured the method exists, so this case mainly guards against the method being deleted between steps or a code bug |

### 4.7 Cross-cutting

| # | Case | Handling |
|---|---|---|
| X1 | What does "stop for manual review" mean, mechanically? | Not defined operationally in the brief — must specify: halt the *entire* run (not just the current item, since downstream totals depend on every line), capture screenshot + UIA tree snapshot + structured reason, exit non-zero, leave Fakturama state as-is (don't rollback partial saves — master data created so far is legitimate and reusable on retry) |
| X2 | Partial-run recovery | Master data (Debtor/Product/VAT/Payment Method) creation is idempotent via exact-match search and safe to resume through. Order/Invoice creation is not idempotent by default — covered by O4's pre-flight duplicate check |
| X3 | LLM extraction non-determinism | Same image, two runs, slightly different JSON (address line-splitting, rounding). Mitigate: temperature 0 / deterministic mode, strict schema validation, and for money-bearing fields consider a self-consistency pass (extract twice, diff, flag disagreement) since these values are the ones least safe to get wrong |
| X4 | PII handling | Order images carry customer PII (name, address, email, phone). If a cloud OCR/LLM API is used, that's data leaving the machine — worth a one-line disclosure in the design doc's tradeoffs section, plus minimizing retention of intermediate images/OCR text |
| X5 | Auditability | Every automated decision (matched-existing vs created-new, which fields were compared for "exact") should be logged — this is also literally what feeds the "annotated screenshots or recording" deliverable |
| X6 | Repeatable testing | UI automation against a stateful desktop app needs a resettable Fakturama data directory/profile per test run, or test runs contaminate each other (a Debtor created in run 1 changes the "0 rows found" branch in run 2) |
| X7 | Extraction provider outage/rate-limit (Claude down, quota hit) | Provider-agnostic interface (§2) means the fallback provider (Gemini) can serve as primary temporarily — but note this changes extraction *behavior*, not just availability, since the two models don't extract identically. Treat a provider failover as a flagged, logged event, not a silent swap |
| X8 | Cross-provider disagreement on money-bearing fields (self-consistency check, §3) | This is a designed halt condition, not a bug: Claude and Gemini disagreeing on a total or VAT% means the source image itself is likely ambiguous to a human too — route to manual review rather than picking either model's answer |
| X9 | API key / credentials for the extraction provider(s) | Never hardcoded; environment-provided. Document in setup instructions which provider(s) need a key and that the fallback provider is optional (system still runs single-provider, just without the cross-check) |

## 5. Open questions / interpretation calls to state explicitly (don't leave implicit)

1. "Exact match" = case/diacritic/whitespace-normalized comparison, applied only for the
   *decision*, never for what gets typed into Fakturama.
2. "Ambiguous/conflicting" = more than one row passing the exact-match predicate, not
   "the raw search returned more than one row."
3. Billing ≠ Delivery address is a real, unspecified-by-the-brief branch that must be
   designed (create second address, assign Delivery role).
4. Payment-code map only covers 3 methods; anything else halts rather than guesses.
5. PAID + missing Payment Date is a contradiction in the brief for that input — halts
   rather than picking a side.
6. Float/currency comparisons use an explicit tolerance, not exact equality.
7. SKU is the sole Product identity key; Name/Description drift doesn't block reuse.
8. A "stop for manual review" halts the whole run, not just the current line item.
9. Alias-not-present needs a deterministic derivation rule, not an ad-hoc LLM guess.
10. Single-instance, sequential execution is assumed; concurrent runs against the same
    Fakturama instance are out of scope.

## 6. Tradeoffs (for the design doc)

- **OCR+rules vs. vision-LLM extraction vs. hybrid.** Pure OCR+regex is fast/cheap/
  deterministic but brittle on layout variance. A vision LLM handles layout variance
  and messy scans well but is non-deterministic and costs latency/money per run, and
  sends the image off-machine. This design goes LLM-native (Claude/Gemini reading the
  image directly) rather than hybrid-with-OCR, since modern vision LLMs already read
  printed text reliably and a separate OCR stage adds a dependency without adding much
  signal — OCR is kept available as an optional secondary signal, not load-bearing. The
  LLM is used to structure the page; independent numeric verification happens in code
  afterward, not inside the LLM call.
- **Claude vs. Gemini vs. Codex.** Claude and Gemini are the two viable choices for the
  runtime extraction call (both are strong multimodal document-understanding models);
  Claude is primary here since the automation is built inside Claude Code, and Gemini
  is the fallback/cross-check provider used for the self-consistency check on
  money-bearing fields (X8) — two *different* models catch a provider-specific
  misreading that re-asking the same model wouldn't. Codex is a code-generation model,
  not a document-understanding one, and doesn't belong in the runtime extraction path —
  its role, if used at all, is as the development-time coding assistant that writes the
  automation code, which is orthogonal to this design.
- **UIA-first vs. vision-based ("look at pixels") automation.** The brief requires UIA/
  OCR/LLM, not hardcoded coordinates. UIA is faster and more robust when the app exposes
  a good accessibility tree; SWT apps (which Fakturama is) are sometimes inconsistent
  about it, so a narrow visual/OCR fallback for the few controls UIA can't reliably
  resolve is reasonable, as long as it still resolves in logical coordinates rather than
  hardcoded pixels.
- **Fail-fast/halt vs. best-effort completion.** Every ambiguous-data corner case above
  resolves toward halting rather than guessing, because this system writes financial
  records. A best-effort mode that pushes through low-confidence data would move faster
  but is the wrong default for anything touching totals, VAT, or payment status.
- **Read-back verification after every write (U7) is expensive but non-negotiable** for
  the same reason — it's the only thing standing between a silently-rejected grid edit
  and a wrong saved Invoice.

## 7. Draft answer — "If you had 3 more hours, what would you do?"

Priority order, roughly matching the corner cases most likely to actually bite in
practice: (1) implement and test the billing≠delivery address branch (E15) since it's
a real gap in the brief; (2) add the pre-flight duplicate-run guard (O4) so the tool is
safe to re-run; (3) build a small synthetic-image test suite covering the ambiguity
cases (D2/D4, P2, E8–E10) to prove the halt-on-ambiguity paths actually halt correctly
rather than just the happy path; (4) tighten the exact-match normalization rules (§5.1)
with real test data instead of a first-pass heuristic; (5) add the self-consistency
double-extraction check (X3) for money-bearing fields.
