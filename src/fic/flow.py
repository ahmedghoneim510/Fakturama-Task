"""The orchestrator: one continuous Order-first flow, brief Steps 1-5.

Each step function: acts, waits for the UI to settle, reads back what actually
persisted, compares it to `source` (the extracted SourceOrder), and either
advances or raises. Nothing advances on an assumption -- only on a confirmed
read-back. RunState is threaded through every step and written to
runs/<id>/state.json after each transition (see report.py) so a failure mid-run
leaves a legible trail of exactly how far it got.

Status of each step in THIS build (be precise, not optimistic -- see README
"what I skipped" for the full picture):
  Step 1 (open order)         -- implemented, live-testable today
  Step 2 (debtor)              -- implemented: select-path + creation branch +
                                   payment-method sub-branch
  Step 3 (product)             -- select-path + VAT/product creation implemented;
                                   line completion (qty/price/discount) depends on
                                   uia/grid.py, which has an explicit, documented
                                   gap on the canvas-grid path
  Step 4 (save + verify order) -- implemented
  Step 5 (invoice)             -- implemented
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from fic.config import date_format, payment_code_for, selectors
from fic.errors import (
    AlreadyProcessedError,
    AmbiguousControl,
    ControlNotFound,
    ManualReviewRequired,
    VerificationFailed,
)
from fic.models import SourceDebtor, SourceItem, SourceOrder, q2
from fic.uia import actions, locator
from fic.uia.session import FakturamaSession
from fic.uia.waits import wait_nested_window_gone, wait_stable, wait_until

SEL = selectors  # config/selectors.yaml, called lazily so tests can monkeypatch


@dataclass
class RunState:
    order_no: str | None = None
    debtor_resolved: bool = False
    payment_method_resolved: bool = False
    products_resolved: list[str] = field(default_factory=list)
    order_saved: bool = False
    invoice_saved: bool = False
    log: list[str] = field(default_factory=list)
    # Per-field differences from the last failed debtor exact-match, so the
    # caller can say WHY re-selection failed rather than merely that it did.
    last_debtor_mismatches: list[dict] = field(default_factory=list)
    # Contacts the DB resolver matched for this debtor's company. If this is
    # non-empty while the UI's five-field test matches nothing, the two
    # disagree -- see try_select_debtor.
    resolver_matches: list[dict] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.log.append(msg)
        # Also emit live. The log used to be collected silently and only shown
        # at the very end, which meant a run could print nothing for minutes --
        # indistinguishable from a hang, and confirmed live to make an operator
        # kill a run that was working fine (it was waiting on Fakturama's cold
        # start). Progress that only appears after success is no help during the
        # part where you actually need it.
        print(f"  · {msg}", flush=True)


def _norm(s: str) -> str:
    return " ".join((s or "").strip().casefold().split())


def _loggable_rows(rows: list[dict]) -> list[dict]:
    """Strip the '_row' UIA wrapper (added by locator.read_table_rows() so
    callers can click a matched row) before a row dict goes into a log
    message or ManualReviewRequired's evidence -- that evidence may get
    logged/serialized (see report.py), and a live pywinauto wrapper object
    has no business there."""
    return [{k: v for k, v in r.items() if k != "_row"} for r in rows]


# --------------------------------------------------------------------------- #
# Step 1 -- open the Order (brief S1.3-1.8)
# --------------------------------------------------------------------------- #


def open_order(session: FakturamaSession, source: SourceOrder, state: RunState):
    """Click Order in the top toolbar, wait for the New Order editor, set the
    header fields, and leave the tab open. Returns the editor container."""
    # Live-confirmed toolbar name: "Create: New Order" (not bare "Order").
    order_btn = locator.find_by_name(session.main_window, "Create: New Order", control_type="Button")
    actions.click(order_btn)

    editor = wait_until(
        lambda: session.wait_for_any_tab("New Order", "Order", timeout=1),
        timeout=15.0,
        description="New Order editor tab",
    )

    no_ctrl = locator.resolve_by_label(editor, "No.", want=("Edit",))
    state.order_no = actions.read_value(no_ctrl)  # leave unchanged -- S1.4
    state.note(f"order opened, proposed No. = {state.order_no!r}")

    # Fail fast if Fakturama has proposed a number it has already used. The
    # brief forbids altering the proposed No. (S1.4), so this cannot be fixed
    # automatically -- but it CAN be reported now rather than after the entire
    # order has been built, which is when Fakturama's own "Error in document
    # number" dialog would otherwise surface it. Confirmed live twice, both
    # times after an unclean shutdown left the number counter behind the data.
    try:
        from fic.uia.contact_resolver import document_number_exists

        if state.order_no and document_number_exists(state.order_no):
            raise ManualReviewRequired(
                f"Fakturama proposed order number {state.order_no!r}, but a document "
                f"with that number already exists -- saving would fail at the very end "
                f"with its own 'Error in document number' dialog. This means its "
                f"number counter is behind its data, which happens when Fakturama is "
                f"force-killed instead of closed with File > Exit. Fix the next number "
                f"in Fakturama's number-range settings (or close it cleanly and "
                f"reopen), then re-run.",
                proposed_number=state.order_no,
            )
    except ManualReviewRequired:
        raise
    except Exception as exc:  # the check is advisory -- never block on its own failure
        state.note(f"could not pre-check the proposed order number -- {exc}")

    # Live-confirmed: "Date" is a 3-segment spinner Pane (month/day/year), NOT
    # an Edit or ComboBox -- resolve it by its own accessible Name directly
    # (S0), not by label-anchored search, which would land on the unrelated
    # Gross/Net dropdown immediately to its right (a real bug caught live: an
    # earlier version of this code wrote dates into that field instead).
    date_ctrl = locator.find_by_name(editor, "Date", control_type="Pane", exact=True)
    actions.set_segmented_date(date_ctrl, source.order_date.strftime(date_format()))

    cust_ref_ctrl = locator.resolve_by_label(
        editor, "Cust.Ref.", aliases=["Kundennr."], want=("Edit",)
    )
    actions.set_text(cust_ref_ctrl, source.external_reference)

    # brief S1.7's price-mode control -- live-confirmed to be the unlabeled
    # ComboBox immediately right of the Date control (currently shows "Gross"
    # by default). It has no accessible label of its own, which is why an
    # earlier version of this code searched for a label "Net" and found
    # nothing -- and, worse, a version before that mis-resolved "Date" itself
    # onto THIS field via label-anchored search, silently overwriting it with
    # typed date text (caught live). Resolved correctly now via position
    # relative to the Date control, not a label.
    r = date_ctrl.rectangle()
    price_mode_ctrl = locator.find_nearest_right(
        editor, (r.left, r.top, r.right, r.bottom), want=("ComboBox",)
    )
    actions.select_combo(price_mode_ctrl, "Net")

    vat_mode_ctrl = locator.resolve_by_label(editor, "VAT", want=("ComboBox",))
    actions.select_combo(vat_mode_ctrl, "With VAT")

    state.note("order header set: Date, Cust.Ref., Net, With VAT")
    return editor


# --------------------------------------------------------------------------- #
# Step 2 -- Debtor (brief S2)
# --------------------------------------------------------------------------- #

# Live-confirmed header order/names in the "Select the address" dialog -- NOT
# what the brief's prose implies ("Company, First Name, Name, ZIP, City"): the
# real dialog has an extra leading "Customer ID" column and calls the surname
# column "Last Name", not "Name".
DEBTOR_DIALOG_COLUMNS = ["Customer ID", "First Name", "Last Name", "Company", "ZIP", "City"]
DEBTOR_MATCH_COLUMNS = ["Company", "First Name", "Last Name", "ZIP", "City"]  # brief S2.3's five


def _debtor_wanted(debtor: SourceDebtor) -> dict[str, str]:
    return {
        "Company": debtor.company,
        "First Name": debtor.first_name or "",
        "Last Name": debtor.last_name or "",
        "ZIP": debtor.billing.zip,
        "City": debtor.billing.city,
    }


def _debtor_exact_matches(rows: list[dict[str, str]], debtor: SourceDebtor) -> list[dict[str, str]]:
    wanted = _debtor_wanted(debtor)
    exact = []
    for row in rows:
        if all(_norm(row.get(col, "")) == _norm(wanted[col]) for col in DEBTOR_MATCH_COLUMNS):
            exact.append(row)
    return exact


def _debtor_mismatch_report(rows: list[dict[str, str]], debtor: SourceDebtor) -> list[dict]:
    """For each candidate row, exactly which of the five fields disagreed.

    "the exact-match search and the just-written values disagree" is a true
    statement and a useless one -- it was the entire error message when a real
    run failed, and answering "disagree HOW?" meant reading the database by
    hand. The comparison already knows which field differs; it just wasn't
    saying. Reporting it turned a live incident into a five-second diagnosis
    (the extraction had no first/last name, so it was comparing '' against
    'Elena'/'Richter').
    """
    wanted = _debtor_wanted(debtor)
    report = []
    for row in rows:
        differing = {
            col: {"expected": wanted[col], "dialog_shows": row.get(col, "")}
            for col in DEBTOR_MATCH_COLUMNS
            if _norm(row.get(col, "")) != _norm(wanted[col])
        }
        if differing:
            report.append(differing)
    return report


def _resolve_search_key(debtor: SourceDebtor, state: RunState) -> str:
    """Prefer a DB-backed resolution over the naive 'search by company' the
    brief's prose suggests: Fakturama's search box does single-string
    substring matching against ONE column at a time, with no cross-column
    AND -- confirmed live to fail for exactly the case that matters most,
    multi-word person names split across FIRSTNAME/NAME. Reading the
    database directly sidesteps that (real columns, real AND, no
    tokenization problem) and additionally proactively catches the
    Name+Street collision Fakturama's own native duplicate check uses,
    BEFORE ever opening a UI dialog that check could otherwise block on.

    This stays advisory, not a replacement for the UI as the existence
    check (R2 / brief S2.13): its ONLY effect is choosing a better search
    string for the UI's own dialog (last name, which the search box is
    confirmed to match correctly, instead of a full company string it
    can't). The actual selection and its read-back verification are
    unchanged below. If the resolver can't run at all (no DB file found,
    wrong machine, parse failure), that's caught here and the flow falls
    back to searching by company exactly as before -- a resolver failure
    degrades the search precision, it never blocks the flow.
    """
    from fic.uia.contact_resolver import ContactResolverError, resolve, search_key_for

    try:
        result = resolve(
            company=debtor.company,
            first_name=debtor.first_name,
            last_name=debtor.last_name,
            zip_code=debtor.billing.zip,
            city=debtor.billing.city,
            street=debtor.billing.street,
        )
    except ContactResolverError as exc:
        state.note(f"contact resolver unavailable ({exc}) -- falling back to company search")
        return debtor.company
    except Exception as exc:  # advisory path -- must never block the flow, whatever fails
        state.note(f"contact resolver failed unexpectedly ({exc!r}) -- falling back to company search")
        return debtor.company

    if result.duplicate_nr_groups:
        state.note(
            f"contact resolver found duplicate Customer ID(s) in the database: "
            f"{list(result.duplicate_nr_groups)} -- NR is not being trusted as unique"
        )

    if len(result.matches) > 1:
        raise ManualReviewRequired(
            "the contact resolver (reading the database directly) found more "
            "than one existing contact matching this debtor -- ambiguous "
            "before ever opening the UI; a human needs to decide which (if "
            "any) is the right one to reuse",
            candidates=[m.__dict__ for m in result.matches],
        )

    state.resolver_matches = [m.__dict__ for m in result.matches]

    if len(result.matches) == 1:
        key = search_key_for(result.matches[0])
        state.note(f"contact resolver found exactly one match (id={result.matches[0].id}); "
                   f"searching UI by {key!r} instead of company")
        return key

    return debtor.company  # resolver found nothing -- search by company as before


def try_select_debtor(
    editor,
    source: SourceOrder,
    state: RunState,
    *,
    search_key: str | None = None,
    confirming: bool = False,
) -> bool:
    """brief S2.1-2.4. Returns True if an existing Debtor was found & selected,
    False if the creation branch is needed. Raises ManualReviewRequired on any
    ambiguous/conflicting result -- per the design docs, "ambiguous" means more
    than one row PASSES THE EXACT-MATCH TEST, not that the raw search returned
    more than one row."""
    # `search_key` overrides the resolver. Used by create_debtor for the
    # post-creation re-selection, where the right thing to search for is the
    # value we JUST WROTE, not whatever the resolver recommends for some
    # other contact. Live failure this fixes: the resolver matched an older
    # contact by company and recommended its surname ("Richter"), but the
    # contact just created had no surname at all (the extraction had none),
    # so searching by "Richter" could never surface it and re-selection was
    # unwinnable.
    search_key = search_key or _resolve_search_key(source.debtor, state)

    # Live-confirmed: the Order editor's own label is "Address" (singular).
    # The Debtor editor's "Addresses" tab (brief S2.7-2.8) is a different label
    # in a different editor -- not yet live-verified, left as the brief states it.
    icon = locator.resolve_anchored_icon(editor, "Address", pick="first_by_top")
    dialog = actions.click(icon, verify_opens="Select the address")

    search = locator.resolve_by_label(dialog, "Search", want=("Edit",))
    actions.set_text(search, search_key, readback=False)

    grid = locator.find_grid(dialog)
    wait_stable(lambda: locator.row_count(grid), description="address search results")
    rows = locator.read_table_rows(grid, DEBTOR_DIALOG_COLUMNS)
    exact = _debtor_exact_matches(rows, source.debtor)

    if len(exact) == 1:
        # Reading a row's text is not the same as selecting it -- confirmed
        # live the hard way: OK with nothing actually clicked/selected in the
        # list closes the dialog and does nothing, no error either. Click the
        # matched row first.
        locator.click_row(exact[0])
        ok = locator.find_by_name(dialog, "OK", control_type="Button")
        actions.click(ok)
        wait_nested_window_gone(dialog)
        state.note(f"debtor selected: exact match on {_loggable_rows([exact[0]])[0]}")
        return True

    if len(exact) > 1:
        raise ManualReviewRequired(
            "more than one existing Debtor exactly matches the extracted values",
            candidates=_loggable_rows(exact),
        )

    cancel = locator.find_by_name(dialog, "Cancel", control_type="Button")
    actions.click(cancel)
    wait_nested_window_gone(dialog)
    state.last_debtor_mismatches = _debtor_mismatch_report(rows, source.debtor)
    detail = ""
    if state.last_debtor_mismatches:
        first = state.last_debtor_mismatches[0]
        detail = " -- closest row differs on " + ", ".join(
            f"{col} (expected {v['expected']!r}, dialog shows {v['dialog_shows']!r})"
            for col, v in first.items()
        )
    # The database says this company already exists, but the UI's five-field
    # test rejected every row. Those two cannot both be right, and creating a
    # customer anyway is the one outcome that does lasting damage: a duplicate
    # record splits a real customer's history and invoices.
    #
    # Live case this prevents: an extraction returned no first/last name, so
    # the five-field test could never match the stored contact, and the run
    # cheerfully created a second "EuroTech Solutions GmbH" with blank names.
    # The resolver had already reported the real one. Nothing was broken except
    # that nothing compared the two answers.
    #
    # `confirming=True` exempts the post-creation re-selection, where the
    # resolver legitimately matches the contact we just made.
    if state.resolver_matches and not confirming:
        raise ManualReviewRequired(
            "the database already holds a contact for this company, but none of the "
            "rows the UI search returned match all five fields -- refusing to create "
            "a duplicate customer. Usually this means the extraction is missing a "
            "field the match depends on (a blank first/last name will never match a "
            "stored row that has one), not that this is genuinely a new customer.",
            already_in_database=state.resolver_matches,
            rows_rejected=state.last_debtor_mismatches,
            wanted=_debtor_wanted(source.debtor),
        )

    state.note(
        f"no exact debtor match among {len(rows)} search result(s)"
        f"{detail} -- creation branch"
    )
    return False


def create_debtor(session: FakturamaSession, editor, source: SourceOrder, state: RunState) -> None:
    """brief S2.5-2.11. Order tab stays open throughout."""
    debtor = source.debtor
    actions.click_menu_item(session.main_window, "New", "New Contact")
    d_editor = wait_until(
        # Live-confirmed real tab name: "New Contact" (Fakturama's own term
        # for what the brief calls the "Debtor" editor -- matches its "Create
        # a new contact" toolbar button and "New Contact" nav entry, both
        # already used elsewhere in this function).
        lambda: session.wait_for_any_tab("New Contact", timeout=1),
        timeout=15.0,
        description="New Debtor editor",
    )

    # Live-confirmed: this version's "Address" sub-tab does NOT match the
    # brief's screenshots field-for-field. Two fields are COMBINED under one
    # label with two side-by-side Edits each ("First Name Last Name", and
    # "ZIP, City") rather than separately labeled; "Country" is plain free
    # text, not a dropdown; there is exactly ONE address block controlled by
    # a single "Delivery Address equals Invoice Address" checkbox, not the
    # brief's separate per-address Invoice/Delivery role checkboxes. Fixed to
    # match what's actually there, live-verified for the identical-address
    # case; the differing-address case below is the one part of this function
    # NOT live-verified -- see README "what I skipped".
    if debtor.company:
        actions.set_text(locator.resolve_by_label(d_editor, "Company", want=("Edit",)), debtor.company)

    first_edit, last_edit = locator.resolve_pair_by_label(d_editor, "First Name Last Name", want=("Edit",))
    if debtor.first_name:
        actions.set_text(first_edit, debtor.first_name)
    if debtor.last_name:
        actions.set_text(last_edit, debtor.last_name)

    b = debtor.billing
    # Live-confirmed: Fakturama's native duplicate-contact check (Name+Street
    # based -- a different, narrower rule than this project's own Company+
    # First+Last+ZIP+City exact-match) can fire live on tab-out of Street,
    # not only at final Save. Its popup then steals keyboard focus, silently
    # corrupting whatever gets typed next (confirmed live: a 'Berlin' write
    # landed as the truncated 'B'). Guarded the same way save() is: snapshot
    # before, check immediately after, stop rather than keep typing into a
    # field that's no longer actually focused.
    before_street = actions.snapshot_top_level_handles()
    actions.set_text(locator.resolve_by_label(d_editor, "Street", want=("Edit",)), b.street)
    dialog_text = actions.check_for_new_dialog(before_street)
    if dialog_text:
        raise ManualReviewRequired(
            f"Fakturama's own duplicate-contact check fired on Street entry: "
            f"{dialog_text!r} -- this needs a human to decide whether to reuse "
            f"the existing contact it found or this is a genuinely different "
            f"person/company at a coincidentally shared address",
            dialog_text=dialog_text,
        )
    zip_edit, city_edit = locator.resolve_pair_by_label(d_editor, "ZIP, City", want=("Edit",))
    actions.set_text(zip_edit, b.zip)
    actions.set_text(city_edit, b.city)
    actions.set_text(locator.resolve_by_label(d_editor, "Country", want=("Edit",)), b.country)

    # Email/Telephone: not found on the "Address" sub-tab in this version
    # (unlike the brief's description) -- their actual location wasn't
    # pinned down live in the time available. Attempted defensively; a miss
    # is logged, not fatal, since neither is required by SourceDebtor's
    # validation and the brief doesn't gate the flow on them.
    for label, val in (("E-Mail", debtor.email), ("Telephone", debtor.phone)):
        if not val:
            continue
        try:
            actions.set_text(locator.resolve_by_label(d_editor, label, want=("Edit",)), val)
        except Exception as exc:
            state.note(f"could not set {label} ({val!r}) -- field not found on Address tab: {exc}")

    # Single checkbox replaces the brief's separate Invoice/Delivery role
    # checkboxes in this version.
    # Live-confirmed: this checkbox carries its label AS its own accessible
    # Name (no separate Text anchor beside it) -- found by name, not by label.
    equals_checkbox = locator.find_by_name(
        d_editor, "Delivery Address equals Invoice Address", control_type="CheckBox"
    )
    if debtor.delivery_same_as_billing:
        _check(equals_checkbox)
    else:
        # Live-confirmed: unchecking reveals a MIRRORED second column to the
        # right (x ~1061-1594) carrying an identical set of labels -- Gender,
        # "First Name Last name", Company, Street, "ZIP, City", Country. Since
        # nothing but horizontal position tells the delivery column from the
        # billing one, every write below pins the anchor with pick="rightmost".
        # (The billing fields above are written BEFORE this uncheck, while only
        # one column exists, so they cannot be caught by the same ambiguity.)
        _check(equals_checkbox, checked=False)
        d = debtor.delivery
        assert d is not None  # delivery_same_as_billing is True when it's None

        # SourceAddress carries ONE combined `name`, while the UI splits into
        # First/Last plus Company. The combined name goes into the delivery
        # Company field -- it is the only free-form single-name field there,
        # and splitting a name this project was given whole would be inventing
        # structure the source document never stated.
        actions.set_text(
            locator.resolve_by_label(d_editor, "Company", want=("Edit",), pick="rightmost"),
            d.name,
        )
        actions.set_text(
            locator.resolve_by_label(d_editor, "Street", want=("Edit",), pick="rightmost"),
            d.street,
        )
        d_zip, d_city = locator.resolve_pair_by_label(
            d_editor, "ZIP, City", want=("Edit",), pick="rightmost"
        )
        actions.set_text(d_zip, d.zip)
        actions.set_text(d_city, d.city)
        actions.set_text(
            locator.resolve_by_label(d_editor, "Country", want=("Edit",), pick="rightmost"),
            d.country,
        )
        state.note(f"separate delivery address written: {d.name}, {d.street}, {d.zip} {d.city}")

    # Miscellaneous sub-tab (brief S2.9) -- must switch tabs first; the
    # fields above live under "Address", which is the default active one.
    misc_tab = locator.find_by_name(d_editor, "Miscellaneous", control_type="TabItem")
    actions.click(misc_tab)
    alias = debtor.alias or _derive_alias(debtor)
    try:
        actions.set_text(locator.resolve_by_label(d_editor, "Alias name", want=("Edit",)), alias)
    except Exception as exc:
        state.note(f"could not set Alias name -- {exc}")
    try:
        actions.set_text(locator.resolve_by_label(d_editor, "Discount", want=("Edit",)), "0%")
    except Exception as exc:
        state.note(f"could not set Discount -- {exc}")
    try:
        actions.select_combo(locator.resolve_by_label(d_editor, "Net or Gross", want=("ComboBox",)), "Net")
    except Exception as exc:
        state.note(f"could not set Net or Gross -- {exc}")

    # Payment: brief describes a separate "Payment" tab (S2.10), but only
    # Address/Miscellaneous/Notice sub-tabs were confirmed live -- tried on
    # Miscellaneous first (where it may live in this version), since raising
    # here would abandon the whole Debtor creation over one field whose real
    # location is unconfirmed rather than known-absent.
    try:
        payment_ctrl = locator.resolve_by_label(d_editor, "Payment", want=("ComboBox",))
        try:
            actions.select_combo(payment_ctrl, source.payment.method)
        except Exception:
            ensure_payment_method(session, source.payment.method, state)
            # ensure_payment_method navigates to the Payments view and opens
            # (and saves) its own editor, so the Contact tab is no longer the
            # active one on return -- the same tab-reactivation requirement as
            # #11, and re-resolving rather than reusing the stale control is
            # what keeps the retry from writing into whatever is active now.
            d_editor = session.wait_for_any_tab("New Contact", timeout=15.0)
            payment_ctrl = locator.resolve_by_label(d_editor, "Payment", want=("ComboBox",))
            actions.select_combo(payment_ctrl, source.payment.method)
    except Exception as exc:
        state.note(
            f"could not set Payment method on the Debtor (tab location unconfirmed "
            f"in this version) -- {exc}. Payment method will still be set explicitly "
            f"on the Invoice in Step 5 regardless (brief S5.2)."
        )

    save_btn = locator.find_by_name(d_editor, "Save", control_type="Button")
    actions.save(d_editor, save_btn, tab_title="New Contact")
    state.note(f"debtor '{debtor.company}' created and saved")

    # Return to the Order, reselect -- successful selection IS the save
    # confirmation (brief S2.13); never a DB peek. Live-confirmed: after Save,
    # Fakturama's active tab is the just-saved Contact, not the Order -- the
    # Order's own fields/icons aren't resolvable until its tab is reactivated
    # (this is exactly the multi-tab scoping limitation session.editor_tab()
    # already documents; explicit reactivation is the mitigation for it here).
    order_editor = session.wait_for_any_tab("New Order", "Order", timeout=15.0)
    # Search by the company we just wrote, not by the resolver's suggestion:
    # the resolver may be pointing at a DIFFERENT existing contact, and the
    # one we need to find is the one that did not exist a moment ago.
    if not try_select_debtor(
        order_editor, source, state, search_key=debtor.company, confirming=True
    ):
        raise ManualReviewRequired(
            "newly created Debtor could not be reselected from the Order -- the "
            "five-field exact match rejected every row the search returned. The "
            "per-field differences are below: a field the extraction left empty "
            "will never match a row that has a value for it.",
            wanted=_debtor_wanted(debtor),
            rows_rejected=state.last_debtor_mismatches,
        )
    state.debtor_resolved = True


def _check(checkbox, *, checked: bool = True) -> None:
    """UIA's TogglePattern (pywinauto: get_toggle_state()/toggle()) is the
    correct way to read/set a checkbox's state -- read_value()'s fallback
    chain (get_value/selected_text/legacy Value/window_text) isn't built for
    ToggleState and would just return the checkbox's own label text via
    window_text(), which is never a reliable signal of checked-vs-not.

    The state is polled rather than read once immediately after toggle():
    live-confirmed that SWT reports the PRE-toggle value on a read taken right
    after the call, so a single read fails on a toggle that in fact worked.
    This bit the Invoice's "paid" box specifically -- there the click also
    triggers a panel re-layout (see complete_and_verify_invoice), giving the
    widget even more reason to lag. Polling for the real condition, rather
    than sleeping a fixed amount and hoping, is the same rule the rest of this
    codebase follows (see uia/waits.py).
    """
    try:
        current = checkbox.get_toggle_state()  # 0=off, 1=on, 2=indeterminate
        if (current == 1) != checked:
            checkbox.toggle()
        try:
            wait_until(
                lambda: True if (checkbox.get_toggle_state() == 1) == checked else None,
                timeout=5.0,
                poll=0.2,
                description="checkbox toggle state to settle",
            )
        except ControlNotFound as exc:
            raise VerificationFailed(
                "checkbox did not read back the intended state",
                wrote=checked,
                read=checkbox.get_toggle_state(),
            ) from exc
    except AttributeError:
        # fallback for a wrapper that doesn't expose TogglePattern for some
        # reason -- best-effort click, unverified
        checkbox.click_input()


def _derive_alias(debtor: SourceDebtor) -> str:
    """Brief S2.9 has no fallback rule for a missing Alias. Deterministic
    derivation rather than an ad-hoc guess: COMPANY-CITY, upper-cased, slugged."""
    raw = f"{debtor.company}-{debtor.billing.city}"
    return "-".join(w.upper() for w in raw.replace("&", "AND").split())


# Live-confirmed header order of the Payments list view. "Standard" comes
# FIRST, exactly like the VATs list (#27) -- and this call site was still
# passing ["Name"], which read_table_rows maps left-to-right, so the STANDARD
# column's (blank) value was being compared against the payment method name.
# It could therefore never match an existing method, and every run created
# another "Bank Transfer". Spotted from a screenshot showing two identical
# rows; the same column-order mistake, in the one place it had not been fixed.
PAYMENT_LIST_COLUMNS = [
    "Standard", "Name", "Description", "Discount", "Discount Days", "Net Days",
]


def ensure_payment_method(session: FakturamaSession, method: str, state: RunState) -> None:
    """brief S2.10.1-2.10.6."""
    # An unmapped payment code is NOT decided here. It used to be: this
    # function refused up front for any method missing from
    # config/app.yaml's payment_code_map, before touching the UI at all.
    #
    # That gate protected nothing on this build. Its Payment editor has no
    # payment-code field whatsoever (only Name, Account, Description, the
    # discount/day fields and the template texts), so the mapped code is never
    # written anywhere -- yet a perfectly ordinary method like "ACH Transfer"
    # was blocked because a lookup table lacked a value that could not have
    # been used. What actually reaches Fakturama, and what the Invoice later
    # selects by, is the method NAME.
    #
    # An unmapped method never halts the run, whether or not the editor has a
    # payment-code field. payment_code_map is a convenience for pre-filling a
    # code, not an allow-list of methods: Fakturama records the method NAME,
    # and the Invoice later selects the method by that name, so the name is
    # written either way. With a field and a mapping, the code is selected;
    # with a field and no mapping, the field keeps the editor's default and
    # the gap is noted; with no field, the mapping is irrelevant and only
    # logged.
    code = payment_code_for(method)

    # Live-confirmed: the navigator entry is "Payments", not "terms of
    # payment" (which is the brief's own wording for the concept, and appears
    # nowhere in this build's UI). The old name matched nothing, so this
    # function could never even reach its search step.
    nav_item = locator.find_by_name(session.main_window, "Payments")
    actions.click(nav_item)
    p_view = wait_until(
        lambda: session.wait_for_any_tab("Payments", timeout=1),
        timeout=10.0,
        description="Payments view",
    )
    search = locator.resolve_by_label(p_view, "Search", want=("Edit",))
    actions.set_text(search, method, readback=False)
    grid = locator.find_grid(p_view, required_headers=("Name",))
    wait_stable(lambda: locator.row_count(grid), description="payment method search results")
    # Ask the grid for its own columns; fall back to the live-confirmed list
    # only if it exposes no headers.
    columns = locator.header_columns(grid) or PAYMENT_LIST_COLUMNS
    rows = locator.read_table_rows(grid, columns)
    exact = [r for r in rows if _norm(r.get("Name", "")) == _norm(method)]

    if len(exact) == 1:
        state.note(f"payment method '{method}' already exists, reusing")
        return
    if len(exact) > 1:
        raise ManualReviewRequired(
            f"multiple existing payment methods named {method!r}", candidates=_loggable_rows(exact)
        )

    # Menu route, for the same reason as ensure_vat's -- see
    # actions.click_menu_item.
    actions.click_menu_item(session.main_window, "New", "New Payment")
    p_editor = session.wait_for_any_tab("New Payment", "Payment", timeout=15.0)

    # Only Name and Description are required, and only they are allowed to
    # abort. Everything between here and the Save below is best-effort by
    # deliberate design, because a half-filled OPTIONAL field must never cost
    # us the save: live-confirmed the hard way -- an exception on the
    # payment-code lookup left a "*New Payment" editor open, filled in and
    # never saved, so the method did not exist when Step 5 went looking for it
    # and the run failed at the very end with "required payment method 'Credit
    # Card' not available on the Invoice". The method had in fact been typed
    # in; nothing had committed it.
    actions.set_text(locator.resolve_by_label(p_editor, "Name", want=("Edit",)), method)
    actions.set_text(locator.resolve_by_label(p_editor, "Description", want=("Edit",)), method)

    # brief S2.10's payment code (S = Standard rate etc.) has NO field on this
    # build's Payment editor -- confirmed against the real editor, whose only
    # controls are Name, Account, Description, Cash discount, Discount Days,
    # Net Days, three template text blocks and a read-only Standard display.
    # Same situation as the TAX Rate editor's missing VAT-code field (#27).
    # Attempted, then logged and skipped rather than treated as fatal.
    try:
        code_ctrl = locator.resolve_by_label(
            p_editor, "payment code", aliases=["Code"], want=("ComboBox",)
        )
    except (ControlNotFound, AmbiguousControl) as exc:
        code_ctrl = None
        mapping = (
            f"the mapped code for {method!r} would have been {code!r}"
            if code is not None
            else f"{method!r} has no entry in payment_code_map, which does not matter here"
        )
        state.note(
            f"no payment-code field on this build's Payment editor ({exc}) -- nothing to set; {mapping}"
        )

    if code_ctrl is not None:
        if code is not None:
            actions.select_combo(code_ctrl, code)
            state.note(f"payment code '{code}' set")
        else:
            # Not a halt -- see the comment at the top of this function. The
            # map pre-fills a code; it does not decide which methods may exist.
            state.note(
                f"payment method {method!r} has no entry in payment_code_map "
                f"(config/app.yaml) -- the payment-code field was left at the "
                f"editor's default; add a mapping if this method needs a specific code"
            )

    # These already default to zero, and they redisplay with units ("0 %"),
    # which means a plain write+read-back comparison reports a false mismatch
    # on a value that was already correct. Compared numerically, written only
    # when actually different, and never fatal.
    for label in ("Cash discount", "Discount Days", "Net Days"):
        try:
            ctrl = locator.resolve_by_label(p_editor, label, want=("Edit",))
            current = re.sub(r"[^0-9.\-]", "", actions.read_value(ctrl)) or "0"
            if Decimal(current) != Decimal(0):
                actions.set_text(ctrl, "0", readback=False)
        except Exception as exc:
            state.note(f"could not confirm {label} on the Payment editor -- {exc}")
    # Set as standard: never clicked -- brief S2.10.5

    save_btn = locator.find_by_name(p_editor, "Save", control_type="Button")
    actions.save(p_editor, save_btn, tab_title="New Payment")
    state.note(f"payment method '{method}' created and saved")


# --------------------------------------------------------------------------- #
# Step 3 -- Products (brief S3), per item, in source order
# --------------------------------------------------------------------------- #


# Live-confirmed real header order (6 columns, not the originally-assumed 3 --
# missing Category/Description shifted every value one+ columns over, though
# harmlessly for the exact-match check below since Item No. was always first).
PRODUCT_DIALOG_COLUMNS = ["Item No.", "Category", "Name", "Description", "Price", "VAT"]


def _open_product_dialog_and_search(editor, sku: str):
    """Live-confirmed anchor: the 'Items' label beside the icon column (not a
    table header -- the inline grid's header cell reads 'Item No.', and this
    icon sits outside that grid entirely, next to its own 'Items' label)."""
    icon = locator.resolve_anchored_icon(editor, "Items", pick="first_by_top")
    dialog = actions.click(icon, verify_opens="Select a product")
    search = locator.resolve_by_label(dialog, "Search", want=("Edit",))
    actions.set_text(search, sku, readback=False)
    grid = locator.find_grid(dialog)
    wait_stable(lambda: locator.row_count(grid), description="product search results")
    rows = locator.read_table_rows(grid, PRODUCT_DIALOG_COLUMNS)
    exact = [r for r in rows if _norm(r.get("Item No.", "")) == _norm(sku)]
    return dialog, exact


def resolve_product_line(session: FakturamaSession, editor, item: SourceItem, state: RunState) -> None:
    dialog, exact = _open_product_dialog_and_search(editor, item.sku)

    if len(exact) > 1:
        raise ManualReviewRequired(
            f"multiple products with SKU {item.sku!r}", candidates=_loggable_rows(exact)
        )

    if not exact:
        cancel = locator.find_by_name(dialog, "Cancel", control_type="Button")
        actions.click(cancel)
        wait_nested_window_gone(dialog)
        ensure_vat(session, item, state)
        create_product(session, item, state)
        # Same tab-reactivation need as create_debtor()'s reselection: after
        # creating the VAT/Product (each in their own tab), the Order tab is
        # no longer active and its icons aren't resolvable until reactivated.
        editor = session.wait_for_any_tab("New Order", "Order", timeout=15.0)
        dialog, exact = _open_product_dialog_and_search(editor, item.sku)
        if not exact:
            raise ManualReviewRequired(f"newly created Product {item.sku!r} not found on reselection")

    # Selecting the row before OK is required -- confirmed live: reading a
    # row's text (which the search/exact-match logic above already did) does
    # not select it, and OK on an unselected list silently does nothing.
    locator.click_row(exact[0])
    ok = locator.find_by_name(dialog, "OK", control_type="Button")
    actions.click(ok)
    wait_nested_window_gone(dialog)

    # Live-confirmed: grid cell-edit activation (complete_line(), right
    # after this) is timing-sensitive -- the row-then-cell click sequence
    # that reliably activates an editor in isolation failed when run
    # immediately after this dialog closed, with no settle time. Poll for
    # the new row to actually appear (a real condition, not a fixed sleep)
    # before handing off to complete_line().
    def _line_present():
        for lst in editor.descendants(control_type="List"):
            try:
                headers = {h.window_text() for h in lst.descendants(control_type="HeaderItem")}
            except Exception:
                continue
            if "Qty." not in headers:
                continue
            for r in lst.descendants(control_type="ListItem"):
                cells = sorted(r.descendants(control_type="Text"), key=lambda c: c.rectangle().left)
                if len(cells) > 2 and _norm(cells[2].window_text()) == _norm(item.sku):
                    return True
        return None

    wait_until(
        _line_present, timeout=10.0, poll=0.2, description=f"line for {item.sku} to settle in the grid"
    )

    state.products_resolved.append(item.sku)
    state.note(f"product '{item.sku}' selected into the Order")
    # Line completion (Qty/U.Price/VAT/Discount) requires the grid strategy in
    # uia/grid.py, which has a documented, unresolved gap on the canvas path --
    # see README "what I skipped". Not silently skipped: complete_line() raises
    # explicitly rather than pretending the line is fully entered.
    complete_line(editor, item, state)


# Live-confirmed real header order of the VATs list view. The original
# ["Name", "Value", "Standard"] was wrong in both membership and order (it
# omitted Description entirely and put Standard last instead of first), which
# silently shifted every value one column over: the existence check compared
# the Standard column's blank marker against the wanted VAT name, so it could
# never match ANY row. Every run therefore fell through to the creation
# branch -- which then failed on its own separate bug (the '+' button, see
# locator.find_list_toolbar_buttons). Two bugs stacked, each hiding the other.
VAT_LIST_COLUMNS = ["Standard", "Name", "Description", "Value"]

# Live-confirmed: Fakturama calls this editor a "TAX Rate", not a "VAT" --
# the tab that opens is "New TAX Rate" (dirty-marked "*New TAX Rate").
# Waiting for a tab called "VATs" (the LIST view's name) would never match.
VAT_EDITOR_TAB = "New TAX Rate"


def _vat_value_matches(displayed: str, pct: Decimal) -> bool:
    """Compare a VAT row's displayed Value ("19 %", "0 %", "20.00 %") against
    the wanted percentage NUMERICALLY.

    String comparison was wrong in both directions: it had to know Fakturama's
    display formatting, and it was fed `str(pct.normalize())`, which renders 20
    as "2E+1" and so could never match anything. A numeric compare is immune to
    both.
    """
    raw = re.sub(r"[^0-9.\-]", "", displayed or "")
    if not raw:
        return False
    try:
        return Decimal(raw) == pct
    except InvalidOperation:
        return False


def ensure_vat(session: FakturamaSession, item: SourceItem, state: RunState) -> None:
    """brief S3.4-3.6 -- resolved before New product so the rate is available in
    its VAT dropdown."""
    vat_name = item.vat_name()
    nav_item = locator.find_by_name(session.main_window, "VATs")
    actions.click(nav_item)
    v_view = wait_until(
        lambda: session.wait_for_any_tab("VATs", timeout=1),
        timeout=10.0,
        description="VATs view",
    )
    search = locator.resolve_by_label(v_view, "Search", want=("Edit",))
    actions.set_text(search, vat_name, readback=False)
    grid = locator.find_grid(v_view, required_headers=("Name", "Value"))
    wait_stable(lambda: locator.row_count(grid), description="VAT search results")
    rows = locator.read_table_rows(grid, VAT_LIST_COLUMNS)
    exact = [
        r
        for r in rows
        if _norm(r.get("Name", "")) == _norm(vat_name)
        and _vat_value_matches(r.get("Value", ""), item.vat_pct)
    ]
    if len(exact) == 1:
        state.note(f"VAT '{vat_name}' already exists, reusing")
        return
    if len(exact) > 1:
        raise ManualReviewRequired(
            f"multiple conflicting VAT rows for {vat_name!r}", candidates=_loggable_rows(exact)
        )

    # Creation via the menu bar, NOT the small add button above the list: those
    # buttons are live-confirmed to expose no name, no automation id and no
    # help text, and clicking one produces only a transient tooltip, never an
    # editor (see actions.click_menu_item and locator.find_list_toolbar_buttons
    # for the full finding). "New > New VAT" is a real, uniquely-named item.
    actions.click_menu_item(session.main_window, "New", "New VAT")
    v_editor = session.wait_for_any_tab(VAT_EDITOR_TAB, "TAX Rate", timeout=15.0)

    actions.set_text(locator.resolve_by_label(v_editor, "Name", want=("Edit",)), vat_name)
    actions.set_text(locator.resolve_by_label(v_editor, "Description", want=("Edit",)), vat_name)
    actions.set_text(
        # vat_pct_str(), never str(...normalize()) -- the latter yields "2E+1"
        # for 20 and Fakturama stores that as 0. See SourceItem.vat_pct_str.
        locator.resolve_by_label(v_editor, "Value", want=("Edit",)), item.vat_pct_str()
    )
    # The brief's third reuse condition -- VAT code == "S (Standard rate)" --
    # is not applicable to this build: the TAX Rate editor has no VAT-code
    # field at all (its only fields are Name, Category, Description, Value,
    # and a read-only Standard display beside a disabled "Set as standard"
    # button). Nothing is written for it rather than inventing a field, and
    # the reuse check above correspondingly tests only Name + Value.
    # "Set as standard" is never clicked -- brief S3.6.
    # Read the Value back BEFORE saving. This is the guard that would have
    # caught the "2E+1" bug at the point of writing instead of three steps
    # later, as a mystifying "line VAT '0 %' does not match extracted 20%":
    # a tax rate whose stored value is silently wrong is exactly the kind of
    # error that otherwise propagates into money.
    written = _vat_value_matches(
        actions.read_value(locator.resolve_by_label(v_editor, "Value", want=("Edit",))),
        item.vat_pct,
    )
    if not written:
        raise VerificationFailed(
            f"the TAX Rate editor did not accept {item.vat_pct_str()!r} as its Value "
            f"-- refusing to save a rate named {vat_name!r} that is not actually "
            f"{item.vat_pct}%",
            wrote=item.vat_pct_str(),
            read=actions.read_value(
                locator.resolve_by_label(v_editor, "Value", want=("Edit",))
            ),
        )

    save_btn = locator.find_by_name(v_editor, "Save", control_type="Button")
    actions.save(v_editor, save_btn, tab_title=VAT_EDITOR_TAB)
    state.note(f"VAT '{vat_name}' created with value {item.vat_pct_str()}%")


# Live-confirmed tab title: "New product" (lowercase 'p' -- Fakturama's own
# capitalisation, matched case-insensitively below regardless).
PRODUCT_EDITOR_TAB = "New product"


def create_product(session: FakturamaSession, item: SourceItem, state: RunState) -> None:
    """brief S3.7-3.11.

    Three of this function's field labels were wrong against the real editor
    and only surfaced once the creation branch was actually exercised live:
      - "Price"      -> the real label is "Price (gross)" (and the Edit beside
                        it has NO accessible name of its own, so it can only
                        be reached via that label).
      - "Stock"      -> the real label is "Quantity".
      - "cost price" -> no such field exists on this editor at all; writing
                        "0.00" to it was never possible. Dropped rather than
                        guessed at another location.
    """
    actions.click_menu_item(session.main_window, "New", "New Product")
    p_editor = wait_until(
        lambda: session.wait_for_any_tab(PRODUCT_EDITOR_TAB, "Product", timeout=1),
        timeout=15.0,
        description="New product editor",
    )
    actions.set_text(locator.resolve_by_label(p_editor, "Item Number", want=("Edit",)), item.sku)
    actions.set_text(locator.resolve_by_label(p_editor, "Name", want=("Edit",)), item.description)
    actions.set_text(
        locator.resolve_by_label(p_editor, "Description", want=("Edit",)), item.description
    )
    gross = item.product_master_gross()
    actions.set_text(locator.resolve_by_label(p_editor, "Price (gross)", want=("Edit",)), str(gross))
    actions.select_combo(
        locator.resolve_by_label(p_editor, "VAT", want=("ComboBox",)), item.vat_name()
    )
    # "Quantity" here is the product master's stock level, not this order
    # line's quantity -- the line quantity is set on the Order's Items grid in
    # complete_line(). Left at 0 deliberately: the brief supplies no stock
    # figure and inventing one would write a fact the source document never
    # stated.
    actions.set_text(locator.resolve_by_label(p_editor, "Quantity", want=("Edit",)), "0")
    save_btn = locator.find_by_name(p_editor, "Save", control_type="Button")
    actions.save(p_editor, save_btn, tab_title=PRODUCT_EDITOR_TAB)
    state.note(f"product '{item.sku}' created, gross price {gross}")


def complete_line(editor, item: SourceItem, state: RunState) -> None:
    """brief S3.13-3.16, live-confirmed working end to end: writing "3" into a
    Qty=1 line changed it to Qty=3 AND correctly recalculated
    Price (1.90 $ -> 5.70 $), an independent cross-check that the write
    reached Fakturama's real data model. See uia/grid.py's module docstring
    for the exact activation mechanism (two separate clicks: row, then cell
    -- not a double-click, not F2) discovered by checking Fakturama's own
    source for its editing-support class.
    """
    from fic.uia import grid as griddy

    # Live-confirmed bug: editor.descendants(control_type="List") can return
    # more than one List -- blindly taking [0] silently picked the wrong one
    # in at least one live run (an empty list elsewhere in the tree), making
    # probe_grid_accessibility() report "canvas-drawn" for a grid that IS
    # accessible. Identify the real Items grid by its own header content
    # instead of positional luck.
    region = None
    for candidate in editor.descendants(control_type="List"):
        try:
            headers = {h.window_text() for h in candidate.descendants(control_type="HeaderItem")}
        except Exception:
            continue
        if "Qty." in headers and "Item No." in headers:
            region = candidate
            break
    if region is None:
        raise ControlNotFound("Items grid (a List with Qty./Item No. headers) not found in the Order editor")

    if not griddy.probe_grid_accessibility(region):
        raise ManualReviewRequired(
            "Items grid is canvas-drawn on this Fakturama version -- the "
            "keyboard-driven column-order learner (uia/grid.py) needs to be "
            "finished against a live run before line completion can be trusted; "
            f"line for SKU {item.sku!r} was NOT completed",
        )

    rows = region.descendants(control_type="ListItem")
    idx = len(state.products_resolved) - 1
    if idx >= len(rows):
        raise ControlNotFound(f"expected a grid row at index {idx}, only {len(rows)} present")
    row = rows[idx]

    # S3.13
    griddy.set_cell_via_children(editor, row, "Qty.", str(item.quantity))

    # S3.14 -- confirm U.Price/VAT match; the selected/created Product's own
    # master price and VAT should already match (that's what product
    # resolution was for), so this is a cross-check, writing only if needed.
    current = griddy.read_line_values(row)
    if griddy.numeric(current.get("U.Price", "")) != griddy.numeric(str(item.unit_net_price)):
        griddy.set_cell_via_children(editor, row, "U.Price", str(item.unit_net_price))
    if griddy.numeric(current.get("VAT", "")) != griddy.numeric(str(item.vat_pct)):
        raise ManualReviewRequired(
            f"line VAT {current.get('VAT')!r} does not match extracted "
            f"{item.vat_pct}% -- the selected/created Product's VAT disagrees "
            f"with this order line for SKU {item.sku!r}",
        )

    # S3.15
    griddy.set_cell_via_children(editor, row, "Discount", str(item.discount_pct))

    # S3.16
    values_after = griddy.read_line_values(row)
    expected_price = item.expected_line_net()
    actual_price = griddy.numeric(values_after.get("Price", ""))
    if actual_price != griddy.numeric(str(expected_price)):
        raise ManualReviewRequired(
            f"line Price {values_after.get('Price')!r} does not match expected "
            f"{expected_price} (qty * unit_net * (1 - discount/100)) after entry "
            f"for SKU {item.sku!r}",
        )
    state.note(
        f"line for {item.sku} completed: qty={item.quantity} "
        f"discount={item.discount_pct}% price={values_after.get('Price')}"
    )


# --------------------------------------------------------------------------- #
# Step 4 -- Complete & save the Order (brief S4)
# --------------------------------------------------------------------------- #


def complete_and_save_order(session: FakturamaSession, editor, source: SourceOrder, state: RunState) -> None:
    # Live-confirmed totals block, read from a real 2-line order:
    #   "Total Net" 17.30 $ | "Discount" 0 % | "Shipping" 0.00 $
    #   "VAT" 3.29 $ | "Total" 20.59 $
    # i.e. Total Net = net, VAT = vat, Total = GROSS. All three matched this
    # project's own computed values exactly.
    #
    # The first label is mode-dependent -- "Total Net" under Net price mode,
    # "Total Gross" under Gross -- which is the tail end of a genuinely
    # instructive bug. An earlier version resolved "Total Gross" and compared
    # BOTH it and "Total" against net_total, and that "worked" only because
    # the order was silently stuck in GROSS mode: select_combo's old
    # type-and-verify path never committed the "Net" selection (the same
    # failure later found on the product VAT combo -- see actions.select_combo).
    # That also fully explains what was logged as an unresolved VAT
    # discrepancy: Fakturama showed 1.09 where the formula gave 1.30 because
    # 6.84/1.19*0.19 = 1.09 -- it was computing the VAT *contained in* a
    # figure it considered gross. With the combo fixed the mode is really Net
    # and VAT reconciles exactly, so there is no discrepancy to document any
    # more. "Total Gross" is still accepted as a fallback so a Gross-mode
    # order fails loudly on the comparison rather than on a missing label.
    try:
        net_ctrl = locator.resolve_by_label(
            editor, "Total Net", want=("Edit", "Text"), pick="last_by_top"
        )
    except ControlNotFound:
        net_ctrl = locator.resolve_by_label(
            editor, "Total Gross", want=("Edit", "Text"), pick="last_by_top"
        )
    # want=("Edit","Text") naturally disambiguates from the header's VAT-mode
    # ComboBox (open_order()) without needing a separate "scope" concept --
    # the control-type filter alone is enough here.
    vat_ctrl = locator.resolve_by_label(editor, "VAT", want=("Edit", "Text"), pick="last_by_top")
    total_ctrl = locator.resolve_by_label(
        editor, "Total", want=("Edit", "Text"), pick="last_by_top"
    )

    def _as_decimal(ctrl):
        # This install prints "$"; a differently-localized one might print
        # "€" or nothing -- strip anything that isn't a digit, sign, or dot.
        raw = re.sub(r"[^0-9.\-]", "", actions.read_value(ctrl))
        try:
            return q2(Decimal(raw))
        except InvalidOperation:
            return None

    tol = Decimal("0.02")

    # brief S4: the order-level Shipping charge is part of the total, and it
    # has to be WRITTEN -- it is not implied by anything on the item lines.
    # Found by running a real order that charges shipping: every line was
    # correct, yet all three totals came up short by exactly the shipping
    # amount (940.00 vs 960.00, VAT 178.60 vs 182.40), because Fakturama was
    # never told about it. The brief's own sample ships free, which is why
    # this stayed invisible until a second document exercised it.
    if source.shipping_amount:
        ship_combo = locator.resolve_by_label(
            editor, "Shipping", want=("ComboBox",), pick="last_by_top"
        )
        sr = ship_combo.rectangle()
        ship_amount_ctrl = locator.find_nearest_right(
            editor, (sr.left, sr.top, sr.right, sr.bottom), want=("Edit",)
        )
        # readback=False then compared numerically: the field redisplays with a
        # currency suffix ("20.00 $"), so set_text's plain string comparison
        # would report a false mismatch on a write that worked.
        actions.set_text(ship_amount_ctrl, str(source.shipping_amount), readback=False)
        written = _as_decimal(ship_amount_ctrl)
        if written is None or abs(written - source.shipping_amount) > tol:
            raise VerificationFailed(
                "shipping amount did not read back as written",
                wrote=str(source.shipping_amount),
                read=actions.read_value(ship_amount_ctrl),
            )
        state.note(f"order shipping set to {source.shipping_amount}")

    # Fakturama's "Total Net" is the ITEMS net -- it excludes the shipping
    # charge, even though its "Total" includes it (live-confirmed on an order
    # shipping at 20.00: Total Net 940.00, VAT 182.40, Total 1142.40, i.e.
    # 940 + 20 + 182.40). SourceOrder's `net_total` uses the other common
    # convention and counts shipping in, so the two are compared against
    # different quantities on purpose rather than being made to look alike.
    # The items net is derived from the lines instead of by subtracting
    # shipping, so it stands on its own; reconcile() has already proven the
    # two agree (lines - discount + shipping == net_total).
    lines_sum = q2(sum((i.line_net_total for i in source.items), Decimal(0)))
    expected_items_net = q2(lines_sum * (Decimal(1) - source.order_discount_pct / 100))

    checks = {
        "Total Net": (_as_decimal(net_ctrl), expected_items_net),
        "VAT": (_as_decimal(vat_ctrl), source.vat_total),
        "Total": (_as_decimal(total_ctrl), source.gross_total),
    }
    mismatches = {
        k: {"read": v[0], "expected": v[1]}
        for k, v in checks.items()
        if v[0] is None or abs(v[0] - v[1]) > tol
    }

    # brief S4: the order-level Discount and Shipping must match the source
    # (the sample supplies neither, so both are expected to be zero). Checked
    # rather than assumed: a stray order-level discount would silently change
    # the saved total while every per-line check still passed.
    order_level = {}
    try:
        disc = locator.resolve_by_label(
            editor, "Discount", want=("Edit", "Text"), pick="last_by_top"
        )
        order_level["Discount"] = _as_decimal(disc)
    except (ControlNotFound, AmbiguousControl) as exc:
        state.note(f"could not read order-level Discount -- {exc}")
    if order_level.get("Discount") is not None:
        if abs(order_level["Discount"] - q2(source.order_discount_pct)) > tol:
            mismatches["Discount"] = {
                "read": order_level["Discount"],
                "expected": q2(source.order_discount_pct),
            }

    if mismatches:
        raise ManualReviewRequired(
            "order totals do not match the source document", mismatches=mismatches
        )

    save_btn = locator.find_by_name(editor, "Save", control_type="Button")
    actions.save(editor, save_btn, tab_title="New Order")
    state.order_saved = True
    state.note(
        f"order {state.order_no} saved; totals verified "
        f"(net {checks['Total Net'][0]}, VAT {checks['VAT'][0]}, gross {checks['Total'][0]})"
    )


def followup_invoice(session: FakturamaSession, order_editor, state: RunState):
    """brief S4.6-4.7 -- create the Invoice FROM the saved Order, so the two
    stay linked, rather than via the toolbar's "Create: New Invoice" (which
    would start an unlinked blank document).

    Live-confirmed mechanism, which is not what this function originally
    assumed: the control is not in the Documents view under a label reading
    "Create a follow-up document" (no such text exists in this build). It is a
    Group named **"Create a duplicate"** inside the saved Order editor itself,
    holding four buttons -- Confirmation / Invoice / Delivery Note / Proforma.
    Clicking "Invoice" opens a "*New Invoice" tab that inherits the Cust.Ref,
    address, every item line and all totals from the Order (verified live:
    both lines and 17.30 / 3.29 / 20.59 carried across untouched).

    Resolution is scoped to that Group deliberately: "Invoice" as a bare name
    would also be a plausible match elsewhere in the window, and being explicit
    here documents which of the four follow-up documents the brief wants.
    """
    try:
        panel = locator.find_by_name(
            order_editor, "Create a duplicate", control_type="Group", exact=True
        )
    except ControlNotFound:
        panel = order_editor  # fall back to the whole editor rather than failing outright
    invoice_btn = locator.find_by_name(panel, "Invoice", control_type="Button", exact=True)
    actions.click(invoice_btn)

    invoice_editor = wait_until(
        lambda: session.wait_for_any_tab("New Invoice", "Invoice", timeout=1),
        timeout=15.0,
        description="linked New Invoice editor",
    )
    state.note("linked Invoice opened from the Order's 'Create a duplicate' panel")
    return invoice_editor


# --------------------------------------------------------------------------- #
# Step 5 -- Invoice (brief S5)
# --------------------------------------------------------------------------- #


def complete_and_verify_invoice(
    session: FakturamaSession, invoice_editor, source: SourceOrder, state: RunState
) -> None:
    # brief S5.5 -- the Invoice inherits everything from the Order, so verify
    # the carried-over totals rather than trusting the copy. Confirmed live
    # that all three come across intact, which is exactly why checking is
    # cheap and a silent divergence would otherwise be invisible.
    def _as_decimal(ctrl):
        raw = re.sub(r"[^0-9.\-]", "", actions.read_value(ctrl))
        try:
            return q2(Decimal(raw))
        except InvalidOperation:
            return None

    tol = Decimal("0.02")
    # Same convention as the Order's own check (see complete_and_save_order):
    # Fakturama's "Total Net" is the ITEMS net and excludes shipping, while
    # SourceOrder.net_total includes it.
    lines_sum = q2(sum((i.line_net_total for i in source.items), Decimal(0)))
    expected_items_net = q2(lines_sum * (Decimal(1) - source.order_discount_pct / 100))

    inherited = {
        "Total Net": (
            _as_decimal(
                locator.resolve_by_label(
                    invoice_editor, "Total Net", want=("Edit", "Text"), pick="last_by_top"
                )
            ),
            expected_items_net,
        ),
        "VAT": (
            _as_decimal(
                locator.resolve_by_label(
                    invoice_editor, "VAT", want=("Edit", "Text"), pick="last_by_top"
                )
            ),
            source.vat_total,
        ),
        "Total": (
            _as_decimal(
                locator.resolve_by_label(
                    invoice_editor, "Total", want=("Edit", "Text"), pick="last_by_top"
                )
            ),
            source.gross_total,
        ),
    }
    bad = {
        k: {"read": v[0], "expected": v[1]}
        for k, v in inherited.items()
        if v[0] is None or abs(v[0] - v[1]) > tol
    }
    if bad:
        raise ManualReviewRequired(
            "Invoice totals inherited from the Order do not match the source document",
            mismatches=bad,
        )

    # The payment-method combo has NO label and NO accessible name of its own
    # (live-confirmed) -- `resolve_by_label(..., "Payment")` finds nothing,
    # because no such label exists on this editor. It is the ComboBox on the
    # same row as the "paid" checkbox, immediately to its right, and it
    # defaults to "Pay Cash" -- so this genuinely has to be set, it is never
    # already correct by luck.
    paid_ctrl = locator.find_by_name(invoice_editor, "paid", control_type="CheckBox", exact=True)
    pr = paid_ctrl.rectangle()
    payment_ctrl = locator.find_nearest_right(
        invoice_editor, (pr.left, pr.top, pr.right, pr.bottom), want=("ComboBox",)
    )
    try:
        actions.select_combo(payment_ctrl, source.payment.method)
    except Exception as exc:
        # brief S5.2 -- no creation branch here, unlike the Debtor's payment
        # method: an Invoice offering no such method is a human decision.
        raise ManualReviewRequired(
            f"required payment method {source.payment.method!r} not available on the Invoice",
        ) from exc
    state.note(f"invoice payment method set to {source.payment.method!r}")

    if source.payment.paid_status == "PAID":
        assert source.payment.payment_date is not None  # enforced by SourcePayment validator
        _check(paid_ctrl)

        # Ticking "paid" REPLACES the payment panel's contents, live-confirmed:
        # the unpaid layout shows "Due Days" + "Pay Until", and only once paid
        # is ticked do the two fields the brief actually wants appear -- a date
        # Pane named "at" (not "payment date", which exists nowhere) and an
        # Edit named "Value". They must therefore be resolved AFTER the tick,
        # never before.
        pay_date_ctrl = locator.find_by_name(
            invoice_editor, "at", control_type="Pane", exact=True
        )
        actions.set_segmented_date(
            pay_date_ctrl, source.payment.payment_date.strftime(date_format())
        )

        # "Value" comes prefilled with the invoice total; confirm rather than
        # blindly overwrite (same stance as complete_line's U.Price check), so
        # a disagreement surfaces instead of being papered over by the write.
        value_ctrl = locator.resolve_by_label(invoice_editor, "Value", want=("Edit",))
        current_value = _as_decimal(value_ctrl)
        if current_value is None or abs(current_value - source.gross_total) > tol:
            actions.set_text(value_ctrl, str(source.gross_total))
        state.note(
            f"invoice marked paid on {source.payment.payment_date} "
            f"with value {source.gross_total}"
        )
    else:
        state.note("invoice left unpaid -- no date/value invented, per brief S5.3")

    save_btn = locator.find_by_name(invoice_editor, "Save", control_type="Button")
    actions.save(invoice_editor, save_btn, tab_title="New Invoice")
    state.invoice_saved = True
    state.note("invoice saved")
    # brief S5.7 -- flow ends here. No Delivery, Correction, or Dunning document.


# --------------------------------------------------------------------------- #
# Top-level orchestration
# --------------------------------------------------------------------------- #


def _phase(n: int, title: str) -> None:
    print(f"\n[Phase {n}] {title}", flush=True)


def run_flow(
    session: FakturamaSession, source: SourceOrder, *, force: bool = False
) -> RunState:
    state = RunState()

    # Idempotency pre-flight, BEFORE any UI is touched. Re-running the tool on
    # the same order image must not quietly produce a second Order -- the run
    # would otherwise succeed completely and leave two real documents for one
    # real purchase, which is worse than a visible failure because nothing
    # downstream flags it.
    #
    # Checked against the saved documents rather than the UI: it costs nothing,
    # it works before Fakturama is even open, and it is the same bounded
    # "refuse an unsafe action" role the document-number pre-check uses. The
    # UI remains the authority on selection; this only decides whether to start.
    if not force:
        try:
            from fic.uia.contact_resolver import find_orders_by_reference

            existing = find_orders_by_reference(source.external_reference)
        except Exception as exc:  # advisory -- never block on the check failing
            existing = []
            state.note(f"could not run the idempotency pre-check -- {exc}")
        if existing:
            raise AlreadyProcessedError(
                f"an order with Cust.Ref {source.external_reference!r} has already "
                f"been saved ({', '.join(d['document'] for d in existing)}) -- "
                f"re-running would create a duplicate for the same purchase. "
                f"Pass --force to run anyway.",
                external_reference=source.external_reference,
                existing_documents=existing,
            )
        state.note(
            f"idempotency check: no existing order for Cust.Ref "
            f"{source.external_reference!r}"
        )

    _phase(1, "open the Order and set its header")
    editor = open_order(session, source, state)

    _phase(2, f"resolve the debtor: {source.debtor.company}")
    if not try_select_debtor(editor, source, state):
        create_debtor(session, editor, source, state)
    else:
        state.debtor_resolved = True

    _phase(3, f"resolve {len(source.items)} product line(s)")
    for item in source.items:
        resolve_product_line(session, editor, item, state)

    _phase(4, "verify the order totals and save")
    complete_and_save_order(session, editor, source, state)

    _phase(5, "create and complete the linked Invoice")
    # Guarantee the payment method exists before the Invoice needs to select
    # it. ensure_payment_method searches first and returns immediately if the
    # method is already there, so this is idempotent -- but it is NOT
    # redundant: the creation branch in brief S2.10 only runs while CREATING a
    # debtor, so an order for an existing customer whose payment method was
    # never created would reach S5.2 and fail with "required payment method
    # not available", having had no opportunity to create it. Confirmed live
    # on exactly that path. This is still S2.10's creation branch, merely
    # guaranteed to run; S5.2's rule that the Invoice itself never creates a
    # payment method is untouched.
    try:
        ensure_payment_method(session, source.payment.method, state)
        editor = session.wait_for_any_tab(state.order_no or "Order", "Order", timeout=15.0)
    except ManualReviewRequired:
        raise
    except Exception as exc:
        state.note(f"could not pre-create payment method {source.payment.method!r} -- {exc}")

    invoice_editor = followup_invoice(session, editor, state)
    complete_and_verify_invoice(session, invoice_editor, source, state)
    return state