"""Extraction pipeline: order image -> validated SourceOrder.

Three interchangeable providers read the image: Claude and Gemini (vision LLMs)
and a local OCR engine (fic/ocr.py -- offline, deterministic, template-bound).
Pick one with `--provider` or FIC_PROVIDER; all three return the same validated
SourceOrder, so nothing downstream knows which one ran.

Claude vision reads the image directly (no separate OCR stage)
and returns structured output via a *tool-forced* call -- we hand Claude a JSON Schema
generated from the SourceOrder model and force it to call a single tool with data
matching that schema, then validate the result through pydantic. This is the
tier-1 validation boundary: a pydantic.ValidationError here means the extraction
itself was malformed (blank SKU, PAID with no date, etc.) and becomes an
ExtractionError before any UI interaction happens.

reconcile() is tier-2: cross-field arithmetic that a single field's type can't
express (line totals summing to the order total, VAT summing correctly, tolerance
for legitimate rounding). A reconcile failure is a ManualReviewRequired, not an
ExtractionError -- it's ambiguous/inconsistent *data*, not malformed *structure*.
"""
from __future__ import annotations

import base64
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from fic.errors import ExtractionError, ManualReviewRequired
from fic.models import SourceOrder, q2

DEFAULT_MODEL = os.environ.get("FIC_CLAUDE_MODEL", "claude-opus-5")

# Tolerance for money comparisons -- legitimate rounding between three independently
# computed values (source printed total, our recomputation, Fakturama's own
# recalculation) is expected; exact float/Decimal equality would spuriously fail.
TOLERANCE = Decimal("0.02")
CONFIDENCE_THRESHOLD = 0.80

# D6: source country text -> the value Fakturama's Country dropdown actually expects.
# Extend this table as new source documents surface unmapped values; an unmapped
# country is a ManualReviewRequired, never a guess.
COUNTRY_MAP = {
    "germany": "Germany",
    "deutschland": "Germany",
    "de": "Germany",
    "austria": "Austria",
    "österreich": "Austria",
    "oesterreich": "Austria",
    "at": "Austria",
    "switzerland": "Switzerland",
    "schweiz": "Switzerland",
    "ch": "Switzerland",
}


def normalize_country(raw: str) -> str:
    key = raw.strip().casefold()
    if key not in COUNTRY_MAP:
        raise ManualReviewRequired(
            f"unmapped country {raw!r} -- add to extraction.COUNTRY_MAP or route to review",
            field="debtor.billing.country",
            raw_value=raw,
        )
    return COUNTRY_MAP[key]


SYSTEM_PROMPT = """You extract structured data from a single purchase-order image.

Rules:
- Transcribe values verbatim. Never infer, complete, guess, or "correct" a value that
  is not printed on the document.
- Money and quantities: plain decimal strings, e.g. "250.00", no currency symbol, no
  thousands separator.
- Percentages: numeric only, e.g. "19" not "19%".
- Dates: ISO 8601, "YYYY-MM-DD".
- billing and delivery addresses are ALWAYS extracted separately, even when they look
  identical or very similar -- never assume, always transcribe both blocks as printed.
- payment.paid_status must be exactly "PAID" or "UNPAID". If the document shows any
  other status (partially paid, overdue, refunded, pending, ...), still report the
  status you see verbatim in a note, but set paid_status to whichever of PAID/UNPAID
  is explicitly and unambiguously printed; if neither is unambiguous, omit
  payment_date and set paid_status to "UNPAID" and flag low confidence on
  "payment.paid_status" in extraction_confidence so the pipeline halts for review
  rather than silently mis-marking it paid.
- If a field is genuinely absent from the image, omit it (for optional fields) rather
  than inventing a value.
- Call the record_order tool exactly once with the complete extraction. Report a
  confidence score (0.0-1.0) in extraction_confidence for every money, date, and
  percentage field you are not fully certain you read correctly.
"""


def _order_schema() -> dict:
    schema = SourceOrder.model_json_schema()
    return schema


def _build_tool() -> dict:
    return {
        "name": "record_order",
        "description": "Record the complete structured extraction of the order image.",
        "input_schema": _order_schema(),
    }


def _image_block(image_path: Path) -> dict:
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(image_path.suffix.lower(), "image/png")
    data = base64.standard_b64encode(image_path.read_bytes()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def extract_with_claude(image_path: Path, *, model: str | None = None) -> SourceOrder:
    """Primary extractor. Requires ANTHROPIC_API_KEY in the environment (see
    .env.example). Raises ExtractionError if Claude's output doesn't validate as a
    SourceOrder."""
    import anthropic  # imported lazily so `fic --help` etc. don't need the SDK installed

    client = anthropic.Anthropic()
    tool = _build_tool()
    resp = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_order"},
        messages=[
            {
                "role": "user",
                "content": [
                    _image_block(image_path),
                    {
                        "type": "text",
                        "text": (
                            "Extract the complete order from this image: order header, "
                            "debtor (billing AND delivery addresses, separately), payment "
                            "block, and every item line. Call record_order once."
                        ),
                    },
                ],
            }
        ],
    )
    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise ExtractionError(
            "Claude did not return a tool_use block", raw_response=resp.model_dump()
        )
    return parse_extraction(tool_use.input, source="claude", model=model or DEFAULT_MODEL)


# An ALIAS, deliberately, not a pinned version: "gemini-2.5-pro" was hardcoded
# here and had already been retired ("no longer available to new users") by the
# time a real key was used against it, so the whole Gemini path failed with a
# 404 that looked like a config error. The -latest aliases keep tracking the
# current model instead of silently expiring. Override with FIC_GEMINI_MODEL.
#
# flash rather than pro by default because the pro alias resolves to a model
# whose FREE-TIER quota is exhausted almost immediately (confirmed live: a
# single image extraction returned RESOURCE_EXHAUSTED). flash extracted this
# project's sample order with 46/46 fields matching the golden file, so the
# accuracy cost is not visible on this workload. Set FIC_GEMINI_MODEL=
# gemini-pro-latest on a paid key.
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"


def extract_with_gemini(image_path: Path, *, model: str | None = None) -> SourceOrder:
    """Fallback / cross-check extractor. Requires GEMINI_API_KEY. Same output
    contract as extract_with_claude -- both return a validated SourceOrder, so
    self_consistency_check() doesn't care which one is "primary"."""
    from google import genai  # lazy import
    from google.genai import types as genai_types

    # An explicit request timeout. The SDK's default is None -- i.e. WAIT
    # FOREVER -- and a stalled request therefore never raises, so the retry
    # logic below never fires and the run simply sits there printing nothing.
    # Confirmed live: an extraction hung with no output and no way to tell it
    # apart from a crash. With a timeout, a hang becomes an ordinary transient
    # failure and is retried like any other.
    client = genai.Client(
        http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )
    image_bytes = image_path.read_bytes()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(image_path.suffix.lower(), "image/png")

    chosen = model or os.environ.get("FIC_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    try:
        resp = _gemini_generate(client, chosen, media_type, image_bytes)
    except Exception as exc:  # transport/API failures -> a legible ExtractionError
        raise ExtractionError(
            f"Gemini API call failed for model {chosen!r}: {exc}",
            provider="gemini",
            model=chosen,
            hint=_gemini_hint(exc),
        ) from exc

    text = resp.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("json"):
            text = text[: -4]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"Gemini output was not valid JSON: {exc}", raw_text=text) from exc
    return parse_extraction(
        data, source="gemini", model=model or os.environ.get("FIC_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    )


def extract_with_ocr(image_path: Path, *, model: str | None = None) -> SourceOrder:
    """Local OCR provider: no API key, no network, same output contract as the
    two LLM extractors. `model` is accepted for signature parity and ignored --
    there is one engine. Needs the optional extra: `uv sync --extra ocr`."""
    from fic import ocr  # lazy: the OCR stack is an optional dependency

    print(f"  reading the image with local OCR ({ocr.ENGINE_NAME}) ...", flush=True)
    started = time.monotonic()
    data, notes = ocr.read_order(image_path)
    print(f"  image read in {time.monotonic() - started:.0f}s", flush=True)
    for note in notes:
        print(f"  note: {note}", flush=True)
    return parse_extraction(data, source="ocr", model=ocr.ENGINE_NAME)


# Every value --provider / FIC_PROVIDER accepts. "ocr" is the local engine.
PROVIDERS = ("claude", "gemini", "ocr")
LLM_PROVIDERS = ("claude", "gemini")

GEMINI_RETRIES = int(os.environ.get("FIC_GEMINI_RETRIES", "4"))
# Per-request ceiling in SECONDS (converted to the SDK's milliseconds below).
# A vision call on a full-page order normally lands in 10-30s; 90s is generous
# without letting a stalled connection hold the whole run hostage.
GEMINI_TIMEOUT_S = float(os.environ.get("FIC_GEMINI_TIMEOUT", "90"))
GEMINI_TIMEOUT_MS = int(GEMINI_TIMEOUT_S * 1000)

# How many times to re-read the image when the first reading is missing fields
# the debtor match depends on. 1 is usually enough -- a second reading either
# recovers the field or confirms it genuinely is not on the document.
EXTRACT_RETRIES = int(os.environ.get("FIC_EXTRACT_RETRIES", "1"))
# Treat a still-incomplete extraction as a halt rather than a warning.
EXTRACT_STRICT = os.environ.get("FIC_EXTRACT_STRICT", "").strip().lower() in {"1", "true", "yes"}


def _is_transient(exc: Exception) -> bool:
    """503 UNAVAILABLE means Google's side is overloaded -- the request never
    reached the model, so retrying is not 'hoping for a different answer to the
    same question', it's re-sending a request that was refused before it was
    read. 429 is rate limiting, which is also worth one or two backed-off
    attempts (the API even returns a retryDelay). Timeouts and dropped
    connections are the same class: the request didn't get a verdict, so
    asking again is legitimate rather than wishful.

    Everything else -- a bad key, a retired model, a malformed image -- will
    fail identically forever, and retrying those would just multiply the wait
    before the real message appears.
    """
    msg = str(exc).lower()
    if any(k in msg for k in ("503", "unavailable", "429", "resource_exhausted")):
        return True
    if any(k in msg for k in ("timeout", "timed out", "connection", "temporarily")):
        return True
    # httpx/httpcore raise timeout types whose str() can be empty -- match on
    # the exception's own class name so an empty message doesn't hide it.
    return "timeout" in type(exc).__name__.lower()


def _gemini_generate(client, model: str, media_type: str, image_bytes: bytes):
    """The API call, with bounded exponential backoff on transient failures.

    Added because `gemini-flash-latest` returned 503 "experiencing high demand"
    repeatedly during real use -- often enough that telling the operator to
    re-run the whole pipeline by hand was the wrong answer. Sleeps 2s, 4s, 8s,
    16s between attempts (FIC_GEMINI_RETRIES to change the count, 0 to disable).
    A non-transient error is re-raised on the first attempt, so a wrong key or a
    retired model still fails fast with its proper message.
    """
    attempts = max(1, GEMINI_RETRIES + 1)
    last: Exception | None = None
    print(
        f"  reading the image with {model} "
        f"(up to {GEMINI_TIMEOUT_S:.0f}s per attempt, {attempts} attempts) ...",
        flush=True,
    )
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[
                    {"text": SYSTEM_PROMPT},
                    {"inline_data": {"mime_type": media_type, "data": image_bytes}},
                    {
                        "text": (
                            "Return ONLY a JSON object matching this schema (no prose, no "
                            "markdown fences):\n" + json.dumps(_order_schema())
                        )
                    },
                ],
            )
        except Exception as exc:
            last = exc
            waited = time.monotonic() - started
            if not _is_transient(exc) or attempt == attempts - 1:
                raise
            delay = 2 ** (attempt + 1)
            print(
                f"  attempt {attempt + 1}/{attempts} failed after {waited:.0f}s "
                f"({type(exc).__name__}); retrying in {delay}s -- transient, "
                f"not a problem with your image or key",
                flush=True,
            )
            time.sleep(delay)
        else:
            print(f"  image read in {time.monotonic() - started:.0f}s", flush=True)
            return resp
    raise last  # unreachable; the loop either returns or raises


def _gemini_hint(exc: Exception) -> str:
    """Turn the three API failures actually hit while running this project into
    an actionable sentence instead of a 60-line SDK traceback. All three are
    configuration or capacity problems, not bugs in the extraction, and each
    has a different fix -- which is exactly what a raw stack trace hides.
    """
    msg = str(exc)
    if "NOT_FOUND" in msg or "404" in msg:
        return (
            "that model name is not available to this key -- pinned Gemini versions "
            "get retired (gemini-2.5-pro already was). Set FIC_GEMINI_MODEL in .env "
            "to a '-latest' alias, e.g. gemini-flash-latest."
        )
    if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
        return (
            "free-tier quota exhausted for this model. The pro models exhaust almost "
            "immediately; set FIC_GEMINI_MODEL=gemini-flash-latest in .env, or wait "
            "for the quota window to reset."
        )
    if "UNAVAILABLE" in msg or "503" in msg:
        return (
            "the model is temporarily overloaded on Google's side -- transient, just "
            "retry. Nothing is wrong with the image or the configuration."
        )
    return "check GEMINI_API_KEY in .env and network access to generativelanguage.googleapis.com"


def parse_extraction(data: dict, *, source: str, model: str) -> SourceOrder:
    try:
        return SourceOrder.model_validate(data)
    except ValidationError as exc:
        raise ExtractionError(
            f"{source} extraction failed schema validation",
            provider=source,
            model=model,
            pydantic_errors=exc.errors(),
        ) from exc


def reconcile(order: SourceOrder) -> None:
    """Tier-2 validation gate. Runs before a single UIA click happens. Any failure
    raises ManualReviewRequired -- never silently 'fix' the arithmetic; a wrong OCR
    read that gets auto-corrected to a plausible number is the worst failure mode
    for a system that writes financial records."""
    issues: list[str] = []

    for item in order.items:
        expected = item.expected_line_net()
        if abs(expected - item.line_net_total) > TOLERANCE:
            issues.append(
                f"line {item.position} ({item.sku}): computed net {expected} "
                f"!= printed line total {item.line_net_total}"
            )

    # Order-level discount and shipping are part of the net total, not
    # afterthoughts. An earlier version compared the bare sum of line totals
    # against net_total, which silently assumed both were always zero -- true
    # of the brief's own sample, and false for any real order that charges
    # shipping. Such an order was rejected with "sum of line totals X !=
    # net_total Y", where the difference was exactly the shipping amount:
    # a correct document failing a check that simply didn't model it.
    lines_sum = q2(sum((i.line_net_total for i in order.items), Decimal(0)))
    discounted = q2(lines_sum * (Decimal(1) - order.order_discount_pct / 100))
    expected_net = q2(discounted + order.shipping_amount)
    if abs(expected_net - order.net_total) > TOLERANCE:
        issues.append(
            f"lines {lines_sum} - {order.order_discount_pct}% discount "
            f"+ {order.shipping_amount} shipping = {expected_net} "
            f"!= order net_total {order.net_total}"
        )

    vat_sum = q2(
        sum(
            (
                q2(
                    i.line_net_total
                    * (Decimal(1) - order.order_discount_pct / 100)
                    * i.vat_pct
                    / 100
                )
                for i in order.items
            ),
            Decimal(0),
        )
    )
    # Shipping is normally taxed too, but SourceOrder carries no VAT rate for
    # it. Rather than invent one, infer it ONLY in the unambiguous case where
    # every line shares a single rate; where lines disagree, say so and let a
    # human decide instead of silently picking one.
    if order.shipping_amount:
        rates = {i.vat_pct for i in order.items}
        if len(rates) == 1:
            vat_sum = q2(vat_sum + q2(order.shipping_amount * rates.pop() / 100))
        else:
            issues.append(
                f"shipping {order.shipping_amount} is charged but the order mixes VAT "
                f"rates {sorted(rates)}, so the rate applying to shipping cannot be "
                "inferred from the document -- needs a human"
            )
    if abs(vat_sum - order.vat_total) > TOLERANCE:
        issues.append(f"computed VAT {vat_sum} != order vat_total {order.vat_total}")

    if abs(q2(order.net_total + order.vat_total) - order.gross_total) > TOLERANCE:
        issues.append(
            f"net_total {order.net_total} + vat_total {order.vat_total} "
            f"!= gross_total {order.gross_total}"
        )

    for field, score in order.confidence.items():
        if score < CONFIDENCE_THRESHOLD:
            issues.append(f"low confidence on {field!r}: {score}")

    if issues:
        raise ManualReviewRequired(
            "extraction failed to reconcile against printed totals",
            issues=issues,
        )


def extract_and_reconcile(
    image_path: Path, *, provider: str = "claude", model: str | None = None
) -> SourceOrder:
    """The single entry point flow.py / cli.py should call.

    Beyond the two validation tiers, this adds a COMPLETENESS pass: an
    extraction can be perfectly valid and still have quietly dropped a field
    that matters (see completeness_gaps). When that happens the image is read
    again -- model output is stochastic, and a second reading routinely
    recovers what the first missed.

    The two readings must agree on every money-bearing field. If they don't,
    the document is genuinely ambiguous and that is a halt, not something to
    pick a winner from. If they do agree, the more complete one wins.
    """
    order = _extract_once(image_path, provider=provider, model=model)
    high, low = completeness_gaps(order)

    # Re-reading only helps a stochastic reader. OCR returns the same boxes for
    # the same pixels, so a second pass would just repeat the first.
    attempts_left = EXTRACT_RETRIES if provider in LLM_PROVIDERS else 0
    while high and attempts_left > 0:
        attempts_left -= 1
        print(
            "  extraction looks incomplete: "
            + "; ".join(high)
            + f"\n  re-reading the image to check ({EXTRACT_RETRIES - attempts_left}"
            f"/{EXTRACT_RETRIES}) ...",
            flush=True,
        )
        retry = _extract_once(image_path, provider=provider, model=model)

        diffs = money_diffs(order, retry, "first read", "second read")
        if diffs:
            raise ManualReviewRequired(
                "two readings of this image disagree on money-bearing fields -- the "
                "document is ambiguous enough that two attempts read it differently, "
                "which is exactly the case a human must settle rather than the "
                "automation picking a winner",
                diffs=diffs,
            )

        retry_high, retry_low = completeness_gaps(retry)
        if len(retry_high) < len(high):
            print(
                f"  second read recovered {len(high) - len(retry_high)} missing "
                f"field(s) -- using it",
                flush=True,
            )
            order, high, low = retry, retry_high, retry_low
        else:
            print("  second read found the same gaps -- they are probably real", flush=True)
            break

    if high:
        # Loud, but not fatal by default: a company-only order genuinely has no
        # contact person, and refusing to process one would be wrong. Set
        # FIC_EXTRACT_STRICT=1 to make these a halt instead.
        print("\n  !! INCOMPLETE EXTRACTION -- proceeding, but check these:", flush=True)
        for gap in high:
            print(f"     - {gap}", flush=True)
        print(
            "     This can be legitimate (a company-only order has no contact "
            "person),\n     but it also means the debtor five-field match may not "
            "recognise an\n     existing customer. Set FIC_EXTRACT_STRICT=1 to stop "
            "on this instead.\n",
            flush=True,
        )
        if EXTRACT_STRICT:
            raise ManualReviewRequired(
                "extraction is missing fields that the debtor match depends on, and "
                "FIC_EXTRACT_STRICT is set",
                gaps=high,
            )
    for gap in low:
        print(f"  note: {gap}", flush=True)

    return order


def _extract_once(image_path: Path, *, provider: str, model: str | None) -> SourceOrder:
    if provider == "claude":
        order = extract_with_claude(image_path, model=model)
    elif provider == "gemini":
        order = extract_with_gemini(image_path, model=model)
    elif provider == "ocr":
        order = extract_with_ocr(image_path)
    else:
        raise ValueError(f"unknown provider {provider!r} -- expected one of {', '.join(PROVIDERS)}")
    reconcile(order)
    return order


def money_diffs(a: SourceOrder, b: SourceOrder, label_a: str, label_b: str) -> list[str]:
    """Every money-bearing disagreement between two extractions of the SAME
    image. Shared by the two-provider cross-check and the completeness retry,
    because both need the identical question answered: are these two readings
    of one document telling the same financial story?"""
    diffs: list[str] = []
    for fld in ("net_total", "vat_total", "gross_total"):
        x, y = getattr(a, fld), getattr(b, fld)
        if abs(x - y) > TOLERANCE:
            diffs.append(f"{fld}: {label_a}={x} {label_b}={y}")
    if len(a.items) != len(b.items):
        diffs.append(f"item count: {label_a}={len(a.items)} {label_b}={len(b.items)}")
    else:
        for i, (ai, bi) in enumerate(zip(a.items, b.items)):
            if ai.sku.strip().casefold() != bi.sku.strip().casefold():
                diffs.append(f"item {i} sku: {label_a}={ai.sku!r} {label_b}={bi.sku!r}")
            if abs(ai.line_net_total - bi.line_net_total) > TOLERANCE:
                diffs.append(
                    f"item {i} line_net_total: {label_a}={ai.line_net_total} "
                    f"{label_b}={bi.line_net_total}"
                )
    return diffs


# Fields pydantic cannot require -- a company-only order legitimately has no
# contact person -- but whose absence is worth acting on. HIGH means it changes
# what the automation DOES; LOW is informational only.
def completeness_gaps(order: SourceOrder) -> tuple[list[str], list[str]]:
    """Return (high, low) severity gaps in an otherwise-valid extraction.

    This exists because a model swap silently returned `first_name=None,
    last_name=None` for a debtor a different model read as "Elena Richter" --
    while still returning `elena.richter@example.test`. The extraction was
    structurally valid, so nothing complained, and the damage only appeared
    much later as an unmatchable debtor and a duplicate contact.

    HIGH gaps are the ones that feed the five-field debtor match (§2.3): if
    they are wrong or missing, the flow cannot recognise an existing customer
    and will create a second one.
    """
    high: list[str] = []
    low: list[str] = []
    d = order.debtor

    if not (d.first_name or d.last_name):
        # An email or phone means the document does identify a person, which
        # makes a completely absent name much more likely to be a miss than a
        # genuinely company-only order.
        hint = " (but an email/phone IS present, so the document names someone)" if (
            d.email or d.phone
        ) else ""
        high.append(f"debtor has neither first_name nor last_name{hint}")

    if not order.confidence:
        low.append("model reported no per-field confidence scores")
    if not d.email:
        low.append("debtor.email missing")
    if not d.phone:
        low.append("debtor.phone missing")
    if not d.customer_id_hint:
        low.append("debtor.customer_id_hint missing")
    for item in order.items:
        if not item.unit:
            low.append(f"item {item.position} ({item.sku}) has no unit")
    return high, low


def self_consistency_check(
    image_path: Path, *, primary: str = "claude", secondary: str = "gemini"
) -> SourceOrder:
    """Extract with two providers and diff the money-bearing fields. Disagreement
    is a designed halt (X8): two independent readers disagreeing on a total or
    VAT% means the source image is likely genuinely ambiguous, not just noisy.
    Returns the `primary` extraction if both agree within tolerance.

    Any two of PROVIDERS work. Claude + Gemini is two models; an LLM + "ocr" is
    two different *kinds* of reader -- a language model and a pixel recognizer
    rarely misread the same digit the same way, so their agreement is the
    stronger signal."""
    if primary == secondary:
        raise ValueError(f"cross-check needs two different providers, got {primary!r} twice")
    first = extract_and_reconcile(image_path, provider=primary)
    second = extract_and_reconcile(image_path, provider=secondary)

    diffs = money_diffs(first, second, primary, secondary)
    if diffs:
        raise ManualReviewRequired(
            f"{primary} and {secondary} extractions disagree on money-bearing fields",
            diffs=diffs,
        )
    return first
