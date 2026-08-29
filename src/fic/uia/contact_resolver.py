"""Resolve a Debtor against Fakturama's actual stored contacts, by reading its
HSQLDB `.script` file directly, to work around two real, live-confirmed
limitations of the in-app search box:

  1. Fakturama's "Select the address" search does single-string substring
     matching against one column at a time, with no cross-column AND and no
     tokenization. A query like "Ahmed Ali" only matches if that exact
     substring appears in ONE column -- it fails whenever first and last name
     live in separate FIRSTNAME/NAME columns, which is the normal case.
  2. The displayed "Customer ID" (the NR column) is NOT a reliable unique
     key. Confirmed live, directly in this project's own test data: force-
     restarting Fakturama between runs (a `taskkill`, not a clean shutdown)
     caused it to reissue an already-used NR ('CUST000001') for a genuinely
     different contact row. Fakturama's own native duplicate-contact check
     (Name+Street based) exists precisely because it can't fully trust its
     own NR either -- this module's design leans on that same insight.

IMPORTANT -- this does not replace the UI as the existence check (R2 in the
design docs / the brief's own §2.13: "successful selection from the Order
confirms the save, never a DB peek"). This module is advisory only: it tells
the UI layer WHAT to search for and WHAT to expect, so the search box is
driven with a single reliable token instead of a multi-word query it can't
handle -- the actual selection, and the actual proof a contact exists, still
happens through Fakturama's own dialog with its own read-back verification,
exactly as before. Reading the DB replaces guessing at a search query, not
the UI-driven confirmation step.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# Confirmed live against a real Database.script (Fakturama 1.6.9, this
# project's actual dev/test instance). Derived primarily by parsing the
# CREATE TABLE line at runtime (see _column_order_from_ddl); this is the
# fallback if that line can't be found, e.g. a very different Fakturama
# version's script doesn't restate DDL inline the same way.
FALLBACK_CONTACTS_COLUMNS = [
    "ID", "ACCOUNT", "ACCOUNT_HOLDER", "BANK_CODE", "BANK_NAME", "BIC", "BIRTHDAY",
    "CATEGORY", "CITY", "COMPANY", "COUNTRY", "DATE_ADDED", "DELETED",
    "DELIVERY_BIRTHDAY", "DELIVERY_CITY", "DELIVERY_COMPANY", "DELIVERY_COUNTRY",
    "DELIVERY_FIRSTNAME", "DELIVERY_GENDER", "DELIVERY_NAME", "DELIVERY_STREET",
    "DELIVERY_TITLE", "DELIVERY_ZIP", "DISCOUNT", "EMAIL", "FAX", "FIRSTNAME",
    "GENDER", "IBAN", "MANDAT_REF", "MOBILE", "NAME", "NOTE", "NR", "PAYMENT",
    "PHONE", "RELIABILITY", "STREET", "SUPPLIERNUMBER", "TITLE", "USE_NET_GROSS",
    "VATNR", "VATNRVALID", "WEBSITE", "ZIP",
]

DEFAULT_SCRIPT_CANDIDATES = [
    # Confirmed live on this project's dev machine -- NOT the
    # %USERPROFILE%\Fakturama2\ path this project's own earlier config
    # comments assumed; that was never verified against a real install and
    # turned out to be wrong. Kept as a second candidate in case it's right
    # on a different machine/version.
    Path.home() / "Database" / "Database.script",
    Path.home() / "Fakturama2" / "Database" / "Database.script",
]


# --------------------------------------------------------------------------- #
# Arabic normalization
# --------------------------------------------------------------------------- #

_ARABIC_DIACRITICS = re.compile(
    "[" + "".join(chr(c) for c in range(0x064B, 0x0653)) + "ٰۖ-ۭ" + "]"
)
_TATWEEL = "ـ"

_ARABIC_CHAR_MAP = {
    # Alef forms -> bare alef
    "آ": "ا",  # آ ALEF WITH MADDA ABOVE
    "أ": "ا",  # أ ALEF WITH HAMZA ABOVE
    "إ": "ا",  # إ ALEF WITH HAMZA BELOW
    "ٱ": "ا",  # ٱ ALEF WASLA
    # Hamza-carrying letters -> their base letter (name-matching, not
    # spelling-correct transliteration: good enough to make "Yusuf"-style
    # variants compare equal, which is the actual goal here)
    "ؤ": "و",  # ؤ WAW WITH HAMZA ABOVE -> و
    "ئ": "ي",  # ئ YEH WITH HAMZA ABOVE -> ي
    # Taa marbuta -> haa (common in casual/OCR'd name spelling variance)
    "ة": "ه",  # ة -> ه
    # Alef maqsura -> yaa
    "ى": "ي",  # ى -> ي
}


def normalize_arabic(text: str) -> str:
    """Fold alef/hamza/taa-marbuta/alef-maqsura variants to one canonical
    form, strip tatweel and diacritics, collapse whitespace, casefold. Used
    ONLY for the match comparison -- never for what gets displayed or typed
    into Fakturama, same rule as every other normalization in this project
    (see models.py's _norm)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace(_TATWEEL, "")
    text = _ARABIC_DIACRITICS.sub("", text)
    text = "".join(_ARABIC_CHAR_MAP.get(ch, ch) for ch in text)
    return " ".join(text.split()).casefold()


def _norm_field(text: str) -> str:
    """General-purpose match-normalization for any field (Arabic or not):
    Arabic folding is a no-op on non-Arabic text, so this is safe to apply
    uniformly rather than branching on script."""
    return normalize_arabic(text)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContactRecord:
    """One CONTACTS row, the fields this project's matching needs. `id` is
    the real HSQLDB primary key -- the only field here safe to treat as a
    unique identifier. `nr` (Customer ID) is carried through for display/
    logging only; see the module docstring for why it must not be trusted."""

    id: int
    nr: str
    company: str
    first_name: str
    last_name: str
    street: str
    zip: str
    city: str
    country: str
    deleted: bool


@dataclass
class ResolveResult:
    """Outcome of resolve(). Exactly one of these should drive the caller:
    - len(matches) == 0            -> no existing contact, safe to create
    - len(matches) == 1             -> reuse matches[0], do not create
    - len(matches) > 1              -> ManualReviewRequired, human decides
    `duplicate_nr_groups` is a diagnostic, not a match result: NR values
    shared by more than one non-deleted row, surfaced so this exact
    Fakturama-level data-integrity issue (confirmed live, see module
    docstring) is visible in logs/reports even when it doesn't happen to
    affect this particular resolve() call.
    """

    matches: list[ContactRecord] = field(default_factory=list)
    duplicate_nr_groups: dict[str, list[ContactRecord]] = field(default_factory=dict)
    source_script: Path | None = None


class ContactResolverError(Exception):
    """Raised for resolver-internal failures (script file missing/unreadable/
    unparseable) -- distinct from fic.errors.ManualReviewRequired, which is
    the caller's business-level response to an AMBIGUOUS (not failed) result."""


# --------------------------------------------------------------------------- #
# .script parsing
# --------------------------------------------------------------------------- #


def find_script_file(candidates: list[Path] | None = None) -> Path:
    for path in candidates or DEFAULT_SCRIPT_CANDIDATES:
        if path.exists():
            return path
    raise ContactResolverError(
        "no Database.script found in any candidate location -- set the path "
        "explicitly rather than relying on auto-detection: "
        f"{[str(p) for p in (candidates or DEFAULT_SCRIPT_CANDIDATES)]}"
    )


def _column_order_from_ddl(script_text: str, table: str = "CONTACTS") -> list[str]:
    """Parse 'CREATE MEMORY TABLE PUBLIC.CONTACTS(ID INTEGER ..., NAME
    VARCHAR(256), ...)' to get the real column order, rather than trusting a
    hardcoded list that a different Fakturama version could silently
    invalidate. Falls back to FALLBACK_CONTACTS_COLUMNS (confirmed live
    against this project's own instance) if the DDL line isn't found."""
    m = re.search(rf"CREATE (?:MEMORY|CACHED) TABLE PUBLIC\.{table}\((.*?)\)\s*$", script_text, re.MULTILINE)
    if not m:
        return list(FALLBACK_CONTACTS_COLUMNS)
    body = m.group(1)
    columns = []
    depth = 0
    current = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            columns.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        columns.append("".join(current))
    names = [c.strip().split()[0] for c in columns if c.strip()]
    return names or list(FALLBACK_CONTACTS_COLUMNS)


def _split_sql_values(values_str: str) -> list[str]:
    """Tokenize a 'VALUES(...)' body into raw field strings, respecting SQL
    single-quote string literals (including '' as an escaped quote inside a
    literal) -- a naive comma-split breaks the moment any text field (NOTE,
    for instance) contains a comma."""
    fields: list[str] = []
    current: list[str] = []
    in_string = False
    i = 0
    n = len(values_str)
    while i < n:
        ch = values_str[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and values_str[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                in_string = False
                i += 1
                continue
            current.append(ch)
            i += 1
            continue
        if ch == "'":
            in_string = True
            i += 1
            continue
        if ch == ",":
            fields.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    # Always append the trailing field, even if empty -- a bare `,''` last
    # column (an empty-string value, common in this data: NOTE, ZIP, etc.
    # are frequently blank) has nothing accumulated in `current`, and an
    # `if current:` guard here would silently drop it, desyncing every
    # column read after it. There is always exactly one field after the
    # final comma (or the whole string, if there were no commas at all).
    fields.append("".join(current).strip())
    return fields


def _coerce(raw: str) -> str:
    """VARCHAR fields come through _split_sql_values already de-quoted, and
    non-string fields (booleans, numbers) as bare tokens -- str() is all
    this module needs, since every field it actually compares is textual."""
    return raw


def parse_contacts(script_path: Path | None = None) -> list[ContactRecord]:
    """Read and parse every CONTACTS row from a Database.script file.
    Non-deleted and deleted rows are both returned (deleted=True included) --
    callers that want existence-checking should filter on `.deleted`
    themselves (resolve() does this), so this stays reusable for
    diagnostics/audit uses that want the full picture."""
    path = script_path or find_script_file()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ContactResolverError(f"could not read {path}: {exc}") from exc

    # Also read the write-ahead log. HSQLDB appends new rows to Database.log
    # and only folds them into Database.script at a checkpoint (a clean
    # shutdown), so a contact created MINUTES ago -- including one this very
    # run just created -- is invisible in the .script alone. Confirmed live:
    # after creating a contact, the resolver still reported the older contact
    # as the only match, because the new one existed only in the log.
    # The DDL comes from the .script; the log carries INSERT lines in the same
    # format, so appending its text is enough.
    log_path = path.with_suffix(".log")
    try:
        if log_path.exists():
            text += "\n" + log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass  # the log is a bonus -- never fail the parse over it

    columns = _column_order_from_ddl(text, "CONTACTS")
    try:
        idx = {name: columns.index(name) for name in (
            "ID", "NR", "COMPANY", "FIRSTNAME", "NAME", "STREET", "ZIP", "CITY",
            "COUNTRY", "DELETED",
        )}
    except ValueError as exc:
        raise ContactResolverError(
            f"CONTACTS column layout is missing an expected column: {exc}"
        ) from exc

    records: list[ContactRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("INSERT INTO CONTACTS VALUES(") and not line.startswith(
            "INSERT INTO PUBLIC.CONTACTS VALUES("
        ):
            continue
        inner = line[line.index("(") + 1 : line.rindex(")")]
        raw_fields = _split_sql_values(inner)
        if len(raw_fields) < len(columns):
            continue  # malformed/truncated line -- skip rather than crash the whole parse
        try:
            records.append(
                ContactRecord(
                    id=int(raw_fields[idx["ID"]]),
                    nr=_coerce(raw_fields[idx["NR"]]),
                    company=_coerce(raw_fields[idx["COMPANY"]]),
                    first_name=_coerce(raw_fields[idx["FIRSTNAME"]]),
                    last_name=_coerce(raw_fields[idx["NAME"]]),
                    street=_coerce(raw_fields[idx["STREET"]]),
                    zip=_coerce(raw_fields[idx["ZIP"]]),
                    city=_coerce(raw_fields[idx["CITY"]]),
                    country=_coerce(raw_fields[idx["COUNTRY"]]),
                    deleted=raw_fields[idx["DELETED"]].strip().upper() == "TRUE",
                )
            )
        except (ValueError, IndexError):
            continue  # one malformed row must not take down the whole resolve()
    return records


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def resolve(
    *,
    company: str,
    first_name: str | None,
    last_name: str | None,
    zip_code: str,
    city: str,
    street: str | None = None,
    script_path: Path | None = None,
) -> ResolveResult:
    """Resolve a debtor against the stored contacts, in two tiers.

    **Tier 1 -- exact company name.** Compared case-insensitively and with
    surrounding whitespace ignored, never partially: "Northstar Office GmbH"
    matches "  northstar office GMBH  " and does NOT match "Northstar Office
    GmbH FreshTest". Exactly one such row wins outright and is reused; more
    than one is genuinely ambiguous and goes to manual review.

    This tier exists because the tier-2 rules below, on their own, are too
    eager to declare ambiguity. Confirmed live: three contacts sharing one
    person and street ("Marta Klein, Friedrichstrasse 88") but with clearly
    DIFFERENT companies -- "Northstar Office GmbH", "... FreshTest",
    "... DupTest2" -- were all reported as candidates by the name+street
    mirror, halting a run whose correct answer was never in doubt. A distinct
    company name is the strongest identity signal on a business document, so
    it is consulted first rather than being drowned out by a heuristic meant
    only to predict Fakturama's own duplicate popup.

    Reusing on company alone is safe here for a specific reason worth stating:
    this function only chooses *which string to type into the UI's search box*
    (see flow._resolve_search_key). The actual selection is still gated by
    try_select_debtor's five-field exact match (Company + First + Last + ZIP +
    City) against the real dialog rows, so a tier-1 hit can never by itself
    cause the wrong customer to be billed.

    **Tier 2 -- the original behaviour, unchanged**, used only when no company
    matches exactly: AND-match across Company/First/Last/ZIP/City (the brief's
    §2.3 rule), plus, when `street` is given, any row matching on
    (first_name, last_name, street) alone. That second rule mirrors
    Fakturama's own native duplicate-contact check, so a caller can route to
    manual review BEFORE opening a UI dialog that check would otherwise block
    on (see actions.py's Street-entry guard, added after exactly that
    happened live).
    """
    resolved_path = script_path or find_script_file()
    all_records = parse_contacts(resolved_path)
    live = [r for r in all_records if not r.deleted]

    want_company = _norm_field(company)
    want_first = _norm_field(first_name or "")
    want_last = _norm_field(last_name or "")
    want_zip = _norm_field(zip_code)
    want_city = _norm_field(city)
    want_street = _norm_field(street or "")

    nr_groups: dict[str, list[ContactRecord]] = {}
    for r in live:
        nr_groups.setdefault(r.nr, []).append(r)
    duplicate_nr_groups = {nr: rows for nr, rows in nr_groups.items() if len(rows) > 1 and nr}

    def _result(matches: list[ContactRecord]) -> ResolveResult:
        return ResolveResult(
            matches=matches,
            duplicate_nr_groups=duplicate_nr_groups,
            source_script=resolved_path,
        )

    # Tier 1 -- exact company name. _norm_field already casefolds and collapses
    # whitespace, so this is the case-insensitive, whitespace-insensitive,
    # strictly-equal comparison the tier calls for.
    if want_company:
        company_exact = [r for r in live if _norm_field(r.company) == want_company]
        if company_exact:
            # One match -> reuse it. More than one -> the caller raises
            # ManualReviewRequired, which is correct: two live contacts sharing
            # an exact company name is a real data problem a human must settle.
            return _result(company_exact)

    # Tier 2 -- no exact company match; fall back to the original rules with
    # every safety check intact.
    exact_matches = [
        r
        for r in live
        if _norm_field(r.company) == want_company
        and _norm_field(r.first_name) == want_first
        and _norm_field(r.last_name) == want_last
        and _norm_field(r.zip) == want_zip
        and _norm_field(r.city) == want_city
    ]

    if street:
        name_street_matches = [
            r
            for r in live
            if _norm_field(r.first_name) == want_first
            and _norm_field(r.last_name) == want_last
            and _norm_field(r.street) == want_street
        ]
        # anything caught by the native-duplicate-mirroring check that ISN'T
        # already in exact_matches is a same-person-different-company (or
        # data-entry-drift) case -- fold it in as an additional "match" for
        # the caller's ambiguity handling, since it's exactly the situation
        # that would otherwise surface as Fakturama's own blocking dialog.
        for r in name_street_matches:
            if r not in exact_matches:
                exact_matches.append(r)

    return _result(exact_matches)


def document_number_exists(number: str, script_path: Path | None = None) -> bool:
    """Is `number` already used by a saved document?

    Fakturama proposes the next document number from a counter it persists
    only on CLEAN shutdown, so an unclean exit leaves the counter behind the
    data and it re-proposes a number that already exists. Saving then fails
    with its own "Error in document number" dialog -- but only at the very
    END, after the order has been fully built, every product created and all
    totals verified. Checking the same file the contact resolver already reads
    turns that into an instant, actionable failure instead of several minutes
    of wasted work.

    Deliberately a plain substring test over the whole file rather than a
    parse of DOCUMENTS: the number is quoted and unique enough that this can't
    reasonably false-positive, it needs no column layout, and both the
    checkpointed `.script` and the write-ahead `.log` (which holds saves made
    since the last checkpoint -- exactly the ones a desynced counter collides
    with) are searched. A miss here is harmless: the flow proceeds and
    Fakturama's own dialog still catches it.
    """
    if not number:
        return False
    try:
        base = (script_path or find_script_file())
    except ContactResolverError:
        return False
    needle = f"'{number}'"
    for candidate in (base, base.with_suffix(".log")):
        try:
            if candidate.exists() and needle in candidate.read_text(
                encoding="utf-8", errors="replace"
            ):
                return True
        except OSError:
            continue
    return False


def _read_db_text(script_path: Path | None = None) -> str:
    """The checkpointed script plus the write-ahead log, concatenated.

    Both are needed for the same reason parse_contacts needs both: HSQLDB only
    folds `.log` into `.script` at a checkpoint, so a document saved by the
    previous run -- precisely the one an idempotency check is looking for --
    usually lives in the log alone.
    """
    base = script_path or find_script_file()
    text = base.read_text(encoding="utf-8", errors="replace")
    log_path = base.with_suffix(".log")
    try:
        if log_path.exists():
            text += "\n" + log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return text


def find_orders_by_reference(
    external_reference: str, script_path: Path | None = None
) -> list[dict[str, str]]:
    """Saved documents whose Cust.Ref equals `external_reference`.

    This is the idempotency check: re-running the tool on the same order image
    must not silently produce a second Order. The comparison is done on the
    parsed CUSTOMERREF **column**, not by searching the file for the reference
    string -- a substring search would also match the reference appearing in an
    address, a note, or another document type, and would refuse to run for the
    wrong reason.

    Returns one dict per matching document with its number and type, so the
    caller can name what it found rather than just refusing.
    """
    if not (external_reference or "").strip():
        return []
    try:
        text = _read_db_text(script_path)
    except (ContactResolverError, OSError):
        return []  # advisory: if the DB can't be read, do not block the run

    columns = _column_order_from_ddl(text, "DOCUMENTS")
    try:
        ref_idx = columns.index("CUSTOMERREF")
        name_idx = columns.index("NAME")
        deleted_idx = columns.index("DELETED")
        type_idx = columns.index("BILLINGTYPE") if "BILLINGTYPE" in columns else None
    except ValueError:
        return []

    wanted = _norm_field(external_reference)
    found: list[dict[str, str]] = []
    # One document can appear several times across the two files: HSQLDB's log
    # records a fresh INSERT for a row each time it is rewritten, and the same
    # row may also already be checkpointed into the script. Reporting
    # "PO000016, PO000016, PO000016" would misrepresent one document as three.
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("INSERT INTO DOCUMENTS VALUES(", "INSERT INTO PUBLIC.DOCUMENTS VALUES(")):
            continue
        try:
            fields = _split_sql_values(line[line.index("(") + 1 : line.rindex(")")])
        except ValueError:
            continue
        if len(fields) <= max(ref_idx, name_idx, deleted_idx):
            continue
        if fields[deleted_idx].strip().upper() == "TRUE":
            continue
        if _norm_field(fields[ref_idx]) != wanted:
            continue
        number = fields[name_idx]
        if number in seen:
            continue
        seen.add(number)
        found.append(
            {
                "document": number,
                "customer_ref": fields[ref_idx],
                "type_code": fields[type_idx] if type_idx is not None else "",
            }
        )
    return found


def search_key_for(record: ContactRecord) -> str:
    """The single token to type into Fakturama's UI search box to reliably
    surface this record -- last name, not NR (unreliable, see module
    docstring) and not company (searching by a multi-word or punctuation-
    heavy company string is exactly the case that's fragile). Confirmed live
    that Fakturama's search matches a single word against its column
    correctly; the failure mode is specifically multi-word queries spanning
    two columns, which searching by last name alone sidesteps entirely."""
    return record.last_name or record.company
