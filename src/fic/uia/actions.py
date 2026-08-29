"""Verified interaction primitives. Every write reads back the result and compares
it to what was intended before the flow advances -- this is the single most
important guard in the system: a silently-rejected or coerced SWT field edit would
otherwise propagate straight into a saved total with no trace of having gone wrong.
"""
from __future__ import annotations

from fic.errors import ManualReviewRequired, OptionUnavailable, VerificationFailed
from fic.uia.waits import wait_until


def _norm(v: str | None) -> str:
    return " ".join((v or "").strip().split())


# pywinauto's type_keys() does NOT type a plain string -- it parses SendKeys
# syntax, where these characters are MODIFIERS AND GROUPING, not literals:
#   ^ = Ctrl   + = Shift   % = Alt   ~ = Enter   () = grouping   {} = key names
# To type one literally it must be wrapped in braces ("{%}").
_SENDKEYS_SPECIAL = "{}^+%~()"


def escape_keys(text: str) -> str:
    """Escape a literal string for type_keys().

    Live-confirmed bug, not a theoretical one: writing the VAT name
    "VAT 19%" silently sent Alt+(nothing) instead of a percent sign, so the
    field read back wrong and the whole product-creation branch failed on its
    very first field. The same trap applies to real extracted data -- the
    project's own golden fixture carries the phone number "+49 30 5550 1420",
    whose leading '+' would be typed as a Shift modifier, and any company name
    containing '(' or '&'-style punctuation is equally exposed.

    Everything user/document-derived must go through this before type_keys();
    deliberate key sequences ("{END}", "{BACKSPACE 60}") must NOT.
    """
    return "".join("{" + ch + "}" if ch in _SENDKEYS_SPECIAL else ch for ch in text)


def read_value(ctrl) -> str:
    """UIA's ValuePattern (pywinauto: `.get_value()`) is the correct way to read
    LIVE content for Edit/ComboBox controls. `window_text()` reads the static
    accessible Name instead, which is a trap here: confirmed live, Fakturama's
    Cust.Ref field's Name is permanently the string 'Cust.Ref.' regardless of
    its actual typed content -- window_text() silently returns the label text
    forever, never the value, with no error to signal it's the wrong property.
    get_value() is tried first for everything; it correctly fails (raises) on
    controls that don't implement ValuePattern -- buttons, static text, list
    rows -- where window_text() IS the right (and only) read, since for those
    the Name genuinely equals the displayed content."""
    try:
        val = ctrl.get_value()
        if val is not None:
            return val
    except Exception:
        pass
    try:
        if ctrl.element_info.control_type == "ComboBox" and hasattr(ctrl, "selected_text"):
            return ctrl.selected_text() or ""
    except Exception:
        pass
    try:
        # Legacy MSAA IAccessible bridge -- confirmed live as the ONLY place
        # Fakturama's segmented Date control (a Pane, not an Edit/ComboBox)
        # exposes its real value; UIA ValuePattern isn't implemented on it at
        # all, and window_text() returns its static Name ("Date") forever.
        legacy_val = ctrl.legacy_properties().get("Value")
        if legacy_val:
            return legacy_val
    except Exception:
        pass
    try:
        return ctrl.window_text() or ""
    except Exception:
        return ""


def set_segmented_date(pane_ctrl, value: str) -> None:
    """Fakturama's Date control (brief S1.5, S5.3's payment date) is a native
    3-segment spinner (month/day/year) exposed as a Pane, confirmed live to
    behave nothing like a normal text field:
      - It is NOT free-text: typing separators ("/") or a full date string at
        once corrupts it into a garbled, unrelated date.
      - Select-all+Delete does not clear it; repeated Backspace instead
        decrements whichever segment currently has spin-focus.
      - The only reliable recipe found: click near the control's LEFT edge to
        focus the month segment, type exactly 2 digits, RIGHT arrow to the day
        segment, 2 digits, RIGHT arrow to the year segment, 4 digits, Tab to
        commit. Confirmed live to reproduce the exact intended date.
    `value` must be "MM/DD/YYYY" (2/2/4 digits) -- this is what THIS
    Fakturama install's Date control expects; a different locale's install may
    expect a different segment order (see config/app.yaml locale.date_format).
    Read-back uses read_value(), which is legacy_properties()['Value'] for
    this control -- and that display omits leading zeros (e.g. "7/16/2026"),
    so comparison is done on parsed month/day/year, not the raw string.
    """
    month, day, year = value.split("/")
    r = pane_ctrl.rectangle()
    pane_ctrl.set_focus()
    pane_ctrl.click_input(coords=(5, (r.bottom - r.top) // 2))
    pane_ctrl.type_keys(month.zfill(2), pause=0.1)
    pane_ctrl.type_keys("{RIGHT}", pause=0.1)
    pane_ctrl.type_keys(day.zfill(2), pause=0.1)
    pane_ctrl.type_keys("{RIGHT}", pause=0.1)
    pane_ctrl.type_keys(year.zfill(4), pause=0.1)
    pane_ctrl.type_keys("{TAB}", pause=0.05)

    actual = read_value(pane_ctrl)
    try:
        actual_parts = [p.strip() for p in actual.split("/")]
        matches = [int(a) for a in actual_parts] == [int(month), int(day), int(year)]
    except (ValueError, IndexError):
        matches = False
    if not matches:
        raise VerificationFailed(
            "segmented date control did not read back as written", wrote=value, read=actual
        )


def set_text(ctrl, value: str, *, readback: bool = True) -> None:
    """Click + clear + type + Tab. Two things confirmed live and NOT the first
    thing you'd guess:
      - `.set_focus()` alone (no real click) left some composite widgets
        inert -- a genuine `click_input()` first is needed.
      - `^a{DELETE}` (Ctrl+A, Delete) does NOT reliably clear Fakturama's
        ComboBox-composite fields (confirmed live: repeated writes
        concatenated instead of replacing). `{END}` + a generous run of
        `{BACKSPACE}` does. Plain Edit controls tolerate this sequence fine
        too, so it's used unconditionally rather than branching on type.
    Tab commits the edit, since several SWT fields validate on focus-out.
    """
    ctrl.set_focus()
    ctrl.click_input()
    ctrl.type_keys("{END}", pause=0.03)
    ctrl.type_keys("{BACKSPACE 60}", pause=0.01)
    if value:
        ctrl.type_keys(escape_keys(value), with_spaces=True, pause=0.01)
    ctrl.type_keys("{TAB}", pause=0.03)
    if readback:
        actual = read_value(ctrl)
        if _norm(actual) != _norm(value):
            raise VerificationFailed(
                "field did not read back as written", wrote=value, read=actual
            )


def _combo_open_button(ctrl, caption: str = "open"):
    """Fakturama's combos are SWT `CCombo`s: a text field plus a small
    drop-down Button rendered INSIDE the combo's own rect, whose accessible
    name is literally 'Open' (and becomes 'Close' once the list is showing)."""
    try:
        buttons = ctrl.descendants(control_type="Button")
    except Exception:
        return None
    for b in buttons:
        try:
            if _norm(b.window_text() or "").casefold() == caption:
                return b
        except Exception:
            continue
    return None


def _combo_dropdown_items(ctrl) -> list:
    """The ListItems of `ctrl`'s currently-open drop-down.

    Identified positionally, because the drop-down list is NOT parented under
    the combo in the UIA tree -- it is a sibling popup elsewhere in the window
    (same structural quirk as the grid's transient cell editor). It is matched
    by being horizontally contained within the combo's own span, which is what
    distinguishes it from every other list in the window (the Items grid, for
    instance, is far wider and starts to the right).
    """
    from fic.uia.locator import snapshot

    try:
        r = ctrl.rectangle()
        root = ctrl.top_level_parent()
    except Exception:
        return []
    items = []
    for n in snapshot(root):
        if n.control_type != "ListItem":
            continue
        if n.rect[0] >= r.left - 8 and n.rect[2] <= r.right + 8 and 0 <= n.rect[1] - r.bottom < 400:
            items.append(n)
    items.sort(key=lambda n: n.rect[1])
    return items


def select_combo(ctrl, value: str) -> None:
    """Exact match only, never fuzzy. A missing option is a business decision
    (OptionUnavailable -> creation branch or manual review), not something to
    approximate to the nearest-sounding option.

    Fakturama uses (at least) two different ComboBox implementations, and
    they were NOT distinguishable from the accessibility tree alone --
    confirmed only by trying one live and watching it fail: some are real
    openable dropdowns where `.expand()` + `.texts()` lists genuine options;
    others (the price-mode field beside Date, confirmed live) are text-
    typable pseudo-combos whose `.texts()` returns only an inner "Open"
    button's caption, not real options, and which don't respond to
    `.expand()`/`.select()` meaningfully at all. Rather than trust the first
    approach on every field the code has never individually driven live, this
    tries the real-dropdown path first and falls back to the type-and-verify
    path (same recipe as set_text) whenever the option list looks bogus
    (empty, or not containing the target) -- so a field of the second kind
    fails safe into the mechanism that's actually confirmed to work on it,
    instead of raising OptionUnavailable against a option list that was never
    real options to begin with.
    """
    ctrl.set_focus()

    if _norm(read_value(ctrl)).casefold() == _norm(value).casefold():
        return  # already the selected option -- nothing to do

    # PRIMARY path: open the CCombo's drop-down and click the real option.
    #
    # This exists because the type-and-verify fallback below is actively
    # DANGEROUS on this widget, which was only discovered by checking the
    # saved record rather than trusting the read-back: typing "VAT 19%" into
    # the combo's text field made read_value() return "VAT 19%", so the write
    # "verified" -- but the selection never reached the model, the field
    # reverted on focus-out, and the product saved with VAT "0 %" (Tax-free,
    # the default). A silently wrong tax rate on a saved product is exactly
    # the class of failure this project's read-back rule exists to prevent,
    # and here the read-back itself was the thing being fooled.
    #
    # `.texts()` is no help either: live-confirmed to return ['VAT', 'VAT',
    # 'Close'] -- the label twice and the drop-down button's caption, never
    # the options. The options only exist as ListItems once the list is open.
    opener = _combo_open_button(ctrl)
    if opener is not None:
        try:
            opener.click_input()
            options = wait_until(
                lambda: _combo_dropdown_items(ctrl) or None,
                timeout=5.0,
                poll=0.2,
                description=f"drop-down options for {value!r}",
            )
        except Exception:
            options = []
        available = [n.name for n in options]
        match = next((n for n in options if _norm(n.name).casefold() == _norm(value).casefold()), None)
        if match is not None:
            match.wrapper.click_input()
            actual = read_value(ctrl)
            if _norm(actual).casefold() == _norm(value).casefold():
                return
            raise VerificationFailed(
                "combo option was clicked but did not become the selection",
                wrote=value,
                read=actual,
            )
        if available:
            # The list opened and genuinely does not contain the value -- a
            # real business outcome (creation branch / manual review), not a
            # mechanism failure, so report it without falling through to
            # typing, which would only fake a success.
            close = _combo_open_button(ctrl, "close")
            if close is not None:
                try:
                    close.click_input()
                except Exception:
                    pass
            raise OptionUnavailable(f"{value!r} not among available options", available=available)

    # Legacy paths, kept for combos that are NOT CCombos (no Open button) --
    # e.g. the order header's price-mode field, which was live-verified to
    # work through type-and-verify before this widget's behaviour was
    # understood.
    try:
        ctrl.expand()
    except Exception:
        pass
    try:
        items = [i for i in (ctrl.texts() if hasattr(ctrl, "texts") else []) if i]
    except Exception:
        items = []
    try:
        ctrl.collapse()
    except Exception:
        pass

    match = next((i for i in items if _norm(i) == _norm(value)), None)
    if match is not None:
        try:
            ctrl.select(match)
            actual = read_value(ctrl)
            if _norm(actual) == _norm(value):
                return
        except Exception:
            pass  # fall through to the type-and-verify path below

    # Real-dropdown path didn't produce the value -- try type-and-verify. If
    # this control genuinely is a constrained dropdown with no free-text
    # entry, typing an invalid value simply won't read back as written, and
    # that VerificationFailed is re-raised as OptionUnavailable with whatever
    # options were seen, which is the correct business-outcome signal either way.
    try:
        set_text(ctrl, value, readback=True)
        return
    except VerificationFailed as exc:
        raise OptionUnavailable(
            f"{value!r} not among available options", available=items
        ) from exc


def click(ctrl, *, verify_opens: str | None = None, timeout: float = 10.0):
    """Click a control; if `verify_opens` is given, wait for a nested dialog
    matching that title to appear within the click target's own top-level
    window (Fakturama's dialogs are Window-type descendants of the main
    application window, not separate OS top-level windows -- confirmed live).
    `ctrl.set_focus()` first: a raw coordinate click can land on the wrong
    window entirely if Fakturama isn't actually foregrounded (also confirmed
    live, with VS Code/browser windows overlapping the same screen region).
    """
    try:
        ctrl.set_focus()
    except Exception:
        pass
    root = ctrl.top_level_parent()
    ctrl.click_input()
    if verify_opens:
        from fic.uia.waits import wait_nested_window

        return wait_nested_window(root, verify_opens, timeout=timeout)
    return None


def click_menu_item(window, menu_name: str, item_name: str, *, timeout: float = 10.0):
    """Open a menu-bar menu and click one of its items, both by real
    accessible name.

    This exists because the obvious route -- the small add ('+') button above
    each list view -- turned out to be unusable: live-confirmed, those buttons
    expose NO name, no automation id, no help text and no MSAA Description
    (they are anonymous `Role 43` push buttons), and clicking one produced
    only a transient tooltip window, never an editor. The menu bar is the
    reliable alternative: 'New > New VAT', 'New > New Payment',
    'New > New Product' and 'New > New Contact' are all real, uniquely-named
    MenuItems.

    One non-obvious detail this handles: Fakturama's menu items are present
    in the UIA tree even while their menu is CLOSED, reporting a degenerate
    (0,0) rectangle. Clicking one in that state clicks the screen origin --
    i.e. some other application entirely. So this opens the parent menu first
    and then waits for the target item to acquire a real, non-zero rect
    before clicking it, which is the actual signal that the menu is open.
    """
    from fic.uia.locator import find_by_name

    menu = find_by_name(window, menu_name, control_type="MenuItem", exact=True)
    menu.click_input()

    def _item_ready():
        for mi in window.descendants(control_type="MenuItem"):
            try:
                if _norm(mi.window_text() or "").casefold() != _norm(item_name).casefold():
                    continue
                r = mi.rectangle()
            except Exception:
                continue
            if r.left or r.top:  # non-degenerate rect => the menu really is open
                return mi
        return None

    item = wait_until(
        _item_ready, timeout=timeout, description=f"menu item {menu_name!r} > {item_name!r}"
    )
    item.click_input()
    return item


def snapshot_top_level_handles() -> set[int]:
    """Baseline for detecting an unexpected new top-level window (a native
    dialog outside the nested-Window pattern every other Fakturama dialog
    uses) appearing as a side effect of ANY action, not just Save. Confirmed
    live: Fakturama's duplicate-contact check can fire on tab-out of the
    Street field mid-form, not only at final Save -- the dialog then steals
    focus, silently corrupting whatever field is typed into next (confirmed
    live: a 'Berlin' write landed as 'B', truncated when the popup grabbed
    focus mid-keystroke). Call this before a sequence that might trigger such
    a check, then check_for_new_dialog() after it."""
    from pywinauto import Desktop

    try:
        return {w.handle for w in Desktop(backend="uia").windows()}
    except Exception:
        return set()


def check_for_new_dialog(before_handles: set[int]) -> str | None:
    """Returns the new window's text if a real DIALOG appeared since
    `before_handles` was captured, else None. See snapshot_top_level_handles().

    Tooltips are filtered out, and that filter is load-bearing rather than
    tidiness: SWT renders tooltips as genuine new top-level windows with an
    empty title (first seen in the list-toolbar probing, #22), so the plain
    "any new top-level window" test raised "Save triggered a dialog instead of
    completing: '<untitled>'" on a save that had nothing wrong with it -- a
    false positive that aborts a good save and, worse, tells the operator to
    go inspect a dialog that has already vanished.

    A real SWT MessageBox always has either a title (even the generic
    "Fakturama" of the duplicate-contact popup) or clickable buttons; a
    tooltip has neither. Anything with no title AND no buttons is therefore
    treated as decoration, not a dialog. The check stays deliberately
    conservative in the other direction -- a window with buttons counts as a
    dialog even when untitled -- because missing a real modal is what caused
    the original hang this whole detection exists to prevent.
    """
    from pywinauto import Desktop

    try:
        windows = list(Desktop(backend="uia").windows())
    except Exception:
        return None
    new_handles = {w.handle for w in windows} - before_handles
    if not new_handles:
        return None

    texts = []
    for w in windows:
        if w.handle not in new_handles:
            continue
        try:
            text = (w.window_text() or "").strip()
        except Exception:
            texts.append("<unreadable>")
            continue
        if text:
            texts.append(text)
            continue
        try:
            has_buttons = bool(w.descendants(control_type="Button"))
        except Exception:
            has_buttons = False
        if has_buttons:
            texts.append("<untitled dialog with buttons>")
        # else: an untitled, button-less window -- a tooltip; ignored.
    return "; ".join(texts) if texts else None


def save(editor_window, save_button, *, tab_title: str | None = None) -> None:
    """Click Save exactly once (brief: 'click the toolbar Save control once' --
    repeated in every stage) and verify via the dirty-tab marker rather than
    assuming the click succeeded. SWT/Fakturama marks an unsaved tab with a
    leading '*' in its title -- confirmed live on the actual TabItem elements
    ('*New Order', '*New Contact'), NOT on editor_window's own title, which in
    this codebase is always session.main_window (the whole-app scope every
    editor resolves to -- see session.editor_tab()) and never carries the '*'
    marker regardless of dirty state. An earlier version of this function
    checked editor_window.window_text() directly, which meant the dirty-check
    always passed immediately on the very first poll, verifying nothing --
    caught only by noticing the fix didn't change observed behavior when it
    should have. Fixed to scan the real TabItems instead.

    Also detects a validation/confirmation dialog appearing as a result of
    Save -- confirmed live: Fakturama's OWN duplicate-contact detector (Name+
    Street based, a DIFFERENT match rule than this project's own Company+
    First+Last+ZIP+City exact-match logic) can pop a 'Duplicate Contact'
    dialog on Save even when this project's own existence-check correctly
    found no match. Any dialog appearing during Save is treated as a
    ManualReviewRequired -- clicking through it blind risks confirming a save
    Fakturama itself flagged as a likely duplicate.

    That dialog is confirmed NOT to be a nested Window-type descendant like
    every other Fakturama dialog found so far -- a direct nested search for
    its "OK" button while it was visibly open, live, found nothing. It's a
    genuine separate top-level OS window (a native SWT MessageBox), generically
    titled just "Fakturama" like the app itself, not by its own heading text.
    An earlier version of this function only checked nested descendants and
    consequently never detected it: the dirty-check then polled forever
    against a tab that could never un-dirty while a blocking modal sat
    un-clicked, and the whole run hung rather than failing -- caught live,
    not in review, after watching it actually happen. Detection now diffs the
    set of true top-level windows before/after the click (not a title/content
    guess, since this dialog's title carries no distinguishing text), and the
    outer timeout is a hard ceiling regardless of what either check sees, so
    a miss here degrades to a bounded failure, never another silent hang.
    """
    def _still_dirty() -> bool:
        """Is the tab we just saved still marked dirty?

        `tab_title` scopes this to ONE tab, and that scoping is load-bearing,
        not a refinement. An earlier version asked "does ANY tab carry a '*'",
        which is wrong in precisely the situation this project is always in:
        the brief requires the New Order tab to stay open across every detour,
        and that tab is dirty from the moment its header fields are set. So
        while saving a Contact / Product / TAX Rate, the Order's own '*' was
        always present, `_still_dirty()` could never go False, and the save
        was reported as failed no matter how well it had actually worked.
        That was previously misread as the timeout being too tight and
        "fixed" by raising it from 15s to 30s -- which of course changed
        nothing except how long it took to fail.

        With no `tab_title` the old any-tab behaviour is kept, since that is
        still correct when only one editor is open.
        """
        try:
            tabs = editor_window.descendants(control_type="TabItem")
        except Exception:
            return True
        for tab in tabs:
            try:
                text = (tab.window_text() or "").strip()
            except Exception:
                continue
            if not text.startswith("*"):
                continue
            if tab_title is None:
                return True
            if _norm(text.lstrip("*")).casefold() == _norm(tab_title).casefold():
                return True
        return False

    # Activate the target tab before clicking Save. The toolbar Save button
    # acts on whichever editor is ACTIVE, not on whatever `editor_window`
    # nominally refers to (which is always session.main_window here) -- so a
    # detour that opened another editor silently redirects the save. Confirmed
    # live: creating a Contact triggers the payment-method sub-branch, which
    # opens and saves a "Bank Transfer" payment editor; that tab was left
    # active, so the subsequent Save saved *it* again and the Contact stayed
    # dirty until the timeout. Clicking the intended tab first makes the save
    # target explicit instead of dependent on whatever ran last.
    if tab_title is not None:
        try:
            for tab in editor_window.descendants(control_type="TabItem"):
                try:
                    text = (tab.window_text() or "").strip().lstrip("*")
                except Exception:
                    continue
                if _norm(text).casefold() == _norm(tab_title).casefold():
                    tab.click_input()
                    break
        except Exception:
            pass  # best effort -- the dirty check below is still authoritative

    before_handles = snapshot_top_level_handles()

    def _outcome():
        new_dialog = check_for_new_dialog(before_handles)
        if new_dialog:
            return ("dialog", new_dialog)
        try:
            for w in editor_window.descendants(control_type="Window"):
                try:
                    text = w.window_text() or ""
                except Exception:
                    continue
                if text:  # any nested dialog present during Save is also unexpected here
                    return ("dialog", text)
        except Exception:
            pass
        return None if _still_dirty() else ("saved", None)

    save_button.click_input()
    try:
        # 15s was live-confirmed too tight for a large-payload save (a full
        # Debtor creation with every field set): the save had genuinely
        # succeeded (tab title lost its dirty marker, no dialog) but the
        # timeout fired first. 30s gives real headroom without masking an
        # actually-hung save for long.
        kind, detail = wait_until(_outcome, timeout=30.0, poll=0.3, description="save to complete")
    except Exception as exc:
        raise VerificationFailed(
            "Save was clicked but neither the dirty marker cleared nor any "
            "dialog was detected within the timeout -- check Fakturama "
            "manually before retrying (retrying Save blindly risks a "
            "duplicate document)",
        ) from exc

    if kind == "dialog":
        raise ManualReviewRequired(
            f"Save triggered a dialog instead of completing: {detail!r}", dialog_text=detail
        )
