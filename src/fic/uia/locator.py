"""The grounding engine: resolves semantic descriptors from config/selectors.yaml
against the LIVE UIA tree. No coordinates are ever written down -- every position
used in a click is computed from the tree at the moment of the call.

Two strategies are implemented for this build (S0, S1 in the design docs' cascade),
plus the narrow anchored-icon resolution the brief requires for the two icon-only
toolbar buttons it names explicitly. Ordinal/keyboard-traversal (S2/S3) and a full
vision fallback (S4) are named in the design docs as the next tier and are not
implemented here -- see README 'what I skipped'. Every S1/anchored-icon resolution
still goes through the ambiguity refusal below, which is the load-bearing safety
property, independent of how many strategies exist.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from fic.errors import AmbiguousControl, ControlNotFound

DEFAULT_AMBIGUITY_MARGIN_PX = 12


@dataclass
class Node:
    control_type: str
    name: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom
    enabled: bool
    wrapper: object


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().strip(":")).lower()


def snapshot(container) -> list[Node]:
    """Flatten a pywinauto container's descendants into comparable Nodes. Errors
    on individual elements (stale references, elements that vanish mid-walk) are
    swallowed per-node rather than failing the whole snapshot."""
    nodes: list[Node] = []
    for w in container.descendants():
        try:
            r = w.rectangle()
            nodes.append(
                Node(
                    control_type=w.element_info.control_type,
                    name=w.window_text() or "",
                    rect=(r.left, r.top, r.right, r.bottom),
                    enabled=w.is_enabled(),
                    wrapper=w,
                )
            )
        except Exception:
            continue
    return nodes


def _v_overlap(a: tuple, b: tuple) -> float:
    top, bottom = max(a[1], b[1]), min(a[3], b[3])
    overlap = max(0, bottom - top)
    height = min(a[3] - a[1], b[3] - b[1]) or 1
    return overlap / height


def _h_overlap(a: tuple, b: tuple) -> float:
    left, right = max(a[0], b[0]), min(a[2], b[2])
    overlap = max(0, right - left)
    width = min(a[2] - a[0], b[2] - b[0]) or 1
    return overlap / width


def find_by_name(container, name: str, control_type: str | None = None, *, exact: bool = False):
    """S0: name + control_type match, scoped to `container`. Not exact by
    default: Fakturama's real toolbar names carry accelerator-key suffixes
    confirmed live -- 'Save (Ctrl+S)', 'Print (Ctrl+P)' -- so the default match
    is 'name is a prefix of the control's accessible name', with `exact=True`
    available for the rare case that matters."""
    target = _norm_label(name)
    for w in container.descendants():
        try:
            text = _norm_label(w.window_text() or "")
            matched = text == target if exact else text.startswith(target)
            if not matched:
                continue
            if control_type and w.element_info.control_type != control_type:
                continue
            if not w.is_enabled():
                continue
            return w
        except Exception:
            continue
    raise ControlNotFound(
        f"no enabled control named {name!r}" + (f" ({control_type})" if control_type else "")
    )


def _pick_anchor(anchors: list[Node], pick: str, label: str) -> Node:
    """Choose among several controls carrying the SAME label text.

    Both cases this handles are real and live-confirmed:
      - "last_by_top": the Order editor has a "VAT" label in the header and
        another in the totals block; the totals one is the lower.
      - "rightmost" / "leftmost": the Contact editor's second address, revealed
        by unchecking "Delivery Address equals Invoice Address", is a MIRRORED
        column to the right with an identical set of labels (Company, Street,
        "ZIP, City", Country ...). Nothing but x-position distinguishes the
        delivery column from the billing one, so writing a delivery value is
        only safe when the anchor is chosen explicitly.
    """
    if pick == "last_by_top":
        return max(anchors, key=lambda n: n.rect[1])
    if pick == "rightmost":
        return max(anchors, key=lambda n: n.rect[0])
    if pick == "leftmost":
        return min(anchors, key=lambda n: n.rect[0])
    if pick != "first":
        raise ValueError(f"unknown pick strategy {pick!r} for label {label!r}")
    return anchors[0]


def resolve_by_label(
    container,
    label: str,
    *,
    want: Iterable[str] = ("Edit", "ComboBox", "CheckBox"),
    aliases: Iterable[str] = (),
    direction: str = "right",
    ambiguity_margin_px: int = DEFAULT_AMBIGUITY_MARGIN_PX,
    pick: str = "first",
):
    """S1: label-anchored spatial resolution. Finds the text `label` (or an
    alias), then the nearest enabled control of an acceptable type in `direction`
    from it, on the same visual row (or column, for direction='below'). Refuses
    -- raises AmbiguousControl -- when the two closest candidates are within
    `ambiguity_margin_px` of each other, rather than guessing which one is
    meant. This is what disambiguates fields like 'VAT' appearing twice in one
    editor (header mode combo vs. totals readout) without silently picking one.
    """
    nodes = snapshot(container)
    wanted = {_norm_label(label)} | {_norm_label(a) for a in aliases}

    def _is_label_anchor(n: Node) -> bool:
        return n.control_type in ("Text", "Static") and _norm_label(n.name) in wanted

    # `pick` selects among MULTIPLE controls carrying the same label text --
    # a real situation, not a hypothetical: the Order editor has a "VAT" Text
    # beside the header's VAT-mode combo AND another "VAT" Text in the totals
    # block at the bottom. Defaulting to the first match silently resolved the
    # totals check against the header label and compared the wrong control.
    # "last_by_top" takes the lowest one on screen, which is what identifies
    # the totals block. This is deliberately not a "scope" concept -- it stays
    # a single, explicit choice at the one call site that needs it.
    anchors = [n for n in nodes if _is_label_anchor(n)]
    if not anchors:
        raise ControlNotFound(f"label {label!r} not found in scope")
    anchor = _pick_anchor(anchors, pick, label)

    want_set = set(want)
    scored: list[tuple[int, Node]] = []
    for n in nodes:
        if n.control_type not in want_set or not n.enabled or n is anchor:
            continue
        if direction == "right":
            if n.rect[0] < anchor.rect[2] - 2 or _v_overlap(n.rect, anchor.rect) < 0.5:
                continue
            scored.append((n.rect[0] - anchor.rect[2], n))
        else:  # "below" -- stacked forms
            if n.rect[1] < anchor.rect[3] - 2 or _h_overlap(n.rect, anchor.rect) < 0.3:
                continue
            scored.append((n.rect[1] - anchor.rect[3], n))

    scored.sort(key=lambda c: c[0])
    if not scored:
        raise ControlNotFound(f"no {want_set} control found near label {label!r}")
    if len(scored) > 1 and scored[1][0] - scored[0][0] < ambiguity_margin_px:
        raise AmbiguousControl(
            f"ambiguous control near label {label!r}",
            candidates=[f"{n.control_type} {n.name!r} @ {n.rect}" for _, n in scored[:3]],
        )
    return scored[0][1].wrapper


ICON_CONTROL_TYPES = ("Button", "Image")  # confirmed live: Fakturama exposes the
# Address/Items icon pair (brief S2.1, S3.2) as UIA "Image" elements, not
# "Button" -- click_input() still works on either, since it's a coordinate
# click, not an Invoke-pattern call.


def resolve_anchored_icon(container, anchor_label: str, *, pick: str = "first_by_top"):
    """Resolve an icon-only control relative to a labelled neighbour -- used for
    the two controls the brief calls out explicitly by position ('the upper
    existing-contact icon, not the lower green +'; 'the upper Product-selection
    icon, not the green +'). 'Upper'/'lower' is computed at runtime from the
    anchor's neighbourhood, never a hardcoded coordinate.
    """
    nodes = snapshot(container)
    wanted_anchor = _norm_label(anchor_label)

    def _is_anchor(n: Node) -> bool:
        return n.control_type in ("Text", "Static") and _norm_label(n.name) == wanted_anchor

    anchor = next((n for n in nodes if _is_anchor(n)), None)
    if anchor is None:
        raise ControlNotFound(f"anchor label {anchor_label!r} not found")

    # Live-confirmed layout: the icon pair sits directly below the label, in
    # (or very near) the label's own horizontal span -- NOT just "somewhere
    # within N px vertically", which is loose enough to match an unrelated
    # control clear across a wide window (caught live: a top-right corner
    # button matched a y-only filter against a label near the top-left).
    x_pad = 40
    neighborhood = [
        n
        for n in nodes
        if n.control_type in ICON_CONTROL_TYPES
        and n.enabled
        and n.rect[0] >= anchor.rect[0] - x_pad
        and n.rect[2] <= anchor.rect[2] + x_pad
        and anchor.rect[1] - 5 <= n.rect[1] < anchor.rect[1] + 200
    ]
    if len(neighborhood) < 2:
        raise ControlNotFound(
            f"expected >=2 icons near {anchor_label!r}, found {len(neighborhood)} -- "
            "vision fallback (S4) is not implemented in this build, see README"
        )
    neighborhood.sort(key=lambda n: n.rect[1])
    return (neighborhood[0] if pick == "first_by_top" else neighborhood[-1]).wrapper


def resolve_pair_by_label(
    container, label: str, *, want: Iterable[str] = ("Edit",), pick: str = "first"
):
    """Like resolve_by_label, but returns the TWO nearest matching controls to
    the right of the label, left-to-right -- for the combined-field labels
    confirmed live in this Fakturama version's Contact editor ("First Name
    Last Name" over two side-by-side Edits; "ZIP, City" the same way). Returns
    (left_ctrl, right_ctrl)."""
    nodes = snapshot(container)
    wanted = {_norm_label(label)}

    def _is_anchor(n: Node) -> bool:
        return n.control_type in ("Text", "Static") and _norm_label(n.name) in wanted

    anchors = [n for n in nodes if _is_anchor(n)]
    if not anchors:
        raise ControlNotFound(f"label {label!r} not found in scope")
    anchor = _pick_anchor(anchors, pick, label)

    want_set = set(want)
    scored: list[tuple[int, Node]] = []
    for n in nodes:
        if n.control_type not in want_set or not n.enabled or n is anchor:
            continue
        if n.rect[0] < anchor.rect[2] - 2 or _v_overlap(n.rect, anchor.rect) < 0.5:
            continue
        scored.append((n.rect[0], n))
    scored.sort(key=lambda c: c[0])
    if len(scored) < 2:
        raise ControlNotFound(f"expected 2 controls right of paired label {label!r}, found {len(scored)}")
    return scored[0][1].wrapper, scored[1][1].wrapper


def find_nearest_right(container, anchor_rect: tuple[int, int, int, int], *, want: Iterable[str]):
    """Like resolve_by_label's candidate scoring, but anchored on an arbitrary
    rect instead of requiring a Text-label anchor -- for the rare case a field
    sits next to another CONTROL rather than a label (live-confirmed: the
    order header's Gross/Net price-mode dropdown has no label of its own; it
    sits immediately right of the Date control)."""
    nodes = snapshot(container)
    want_set = set(want)
    scored: list[tuple[int, Node]] = []
    for n in nodes:
        if n.control_type not in want_set or not n.enabled:
            continue
        if n.rect[0] < anchor_rect[2] - 2 or _v_overlap(n.rect, anchor_rect) < 0.5:
            continue
        scored.append((n.rect[0] - anchor_rect[2], n))
    scored.sort(key=lambda c: c[0])
    if not scored:
        raise ControlNotFound(f"no {want_set} control found right of {anchor_rect}")
    if len(scored) > 1 and scored[1][0] - scored[0][0] < DEFAULT_AMBIGUITY_MARGIN_PX:
        raise AmbiguousControl(
            f"ambiguous control right of {anchor_rect}",
            candidates=[f"{n.control_type} {n.name!r} @ {n.rect}" for _, n in scored[:3]],
        )
    return scored[0][1].wrapper


def find_grid(container, *, required_headers: Iterable[str] = ()):
    """Locate the results grid inside a dialog (Select the address / Select a
    product) or a list view (VATs, Payments, Documents).

    `required_headers` disambiguates when more than one grid is reachable in
    the scope -- which is the normal case for the list views, because they are
    searched with `session.main_window` as the scope while the New Order tab
    is still open (the brief requires it to stay open), so the Order's own
    Items grid is in the tree too. Taking the first grid found would then be a
    coin flip; the same positional-luck bug already bit complete_line() once.
    Pass the header captions the wanted grid must have.

    Implemented as ONE unfiltered `snapshot()` pass rather than up to three
    `descendants(control_type=...)` calls. That is a live-confirmed
    performance fix, not a stylistic one: called against a whole editor scope
    (which is what the list views pass -- `session.main_window`, since
    editor_tab() cannot isolate a tab's content pane), the repeated filtered
    walks stalled indefinitely, with no timeout able to fire because the block
    happens INSIDE a single COM call rather than between polls. The
    unfiltered walk over the same scope completes in a fraction of a second
    (measured: ~179 elements for this app's main window). Preference order
    across control types is preserved exactly.
    """
    nodes = snapshot(container)
    wanted = {_norm_label(h) for h in required_headers}

    def _has_headers(wrapper) -> bool:
        if not wanted:
            return True
        try:
            present = {
                _norm_label(h.window_text() or "")
                for h in wrapper.descendants(control_type="HeaderItem")
            }
        except Exception:
            return False
        return wanted.issubset(present)

    for ct in ("DataGrid", "Table", "List"):
        for n in nodes:
            if n.control_type == ct and _has_headers(n.wrapper):
                return n.wrapper
    raise ControlNotFound(
        "no DataGrid/Table/List control found in this container"
        + (f" with headers {sorted(wanted)}" if wanted else "")
    )


def find_list_toolbar_buttons(container) -> list:
    """Resolve the small unnamed add/delete buttons that sit directly above a
    list view's grid (Data > VATs, Payments, Products, Documents ...).

    Live-confirmed, and the reason an earlier version of this code was simply
    wrong: these buttons carry NO accessible name at all -- an empty string,
    not '+' as the brief's screenshots (which show a green plus glyph) and the
    design docs both assumed. `find_by_name(view, "+")` therefore could never
    match, and `ensure_vat`/`ensure_payment_method` failed the moment either
    creation branch was actually exercised.

    Resolution is positional and computed live from the grid's own rect: the
    buttons sit in a narrow band immediately ABOVE the grid, left-aligned with
    its left edge. Verified against two different views in one session (VATs:
    grid left=273, buttons at x=273 and x=296; Documents: grid left=422,
    buttons at x=422 and x=445), which is what establishes it as the view
    layout convention rather than a coincidence of one screen.

    Returned left-to-right, so [0] is add and [1] (when present) is delete.
    """
    grid = find_grid(container)
    g = grid.rectangle()
    candidates: list[Node] = []
    for n in snapshot(container):
        if n.control_type != "Button" or not n.enabled:
            continue
        if (n.name or "").strip():
            continue  # named buttons here are window/app chrome, never these
        above_band = g.top - 60 <= n.rect[3] <= g.top + 5
        left_aligned = g.left - 20 <= n.rect[0] <= g.left + 200
        if above_band and left_aligned:
            candidates.append(n)
    candidates.sort(key=lambda n: n.rect[0])
    return [c.wrapper for c in candidates]


def find_list_add_button(container):
    """The leftmost of find_list_toolbar_buttons() -- the 'add new' control."""
    buttons = find_list_toolbar_buttons(container)
    if not buttons:
        raise ControlNotFound(
            "no unnamed add/delete buttons found above this view's grid -- the "
            "list-view toolbar layout this resolution depends on (see "
            "find_list_toolbar_buttons) did not hold here"
        )
    return buttons[0]


def _header_bounds(grid) -> list[tuple[int, int]]:
    """(left, right) of each header cell, left to right -- the x-ranges that
    define which column a row cell belongs to. Empty if the grid has no
    headers, in which case callers fall back to sequential mapping."""
    try:
        headers = grid.descendants(control_type="HeaderItem")
    except Exception:
        return []
    bounds = []
    for h in headers:
        try:
            r = h.rectangle()
            bounds.append((r.left, r.right))
        except Exception:
            continue
    bounds.sort(key=lambda b: b[0])
    return bounds


def _column_for(cell_rect: tuple[int, int], bounds: list[tuple[int, int]]) -> int | None:
    """Which header column a cell belongs to, decided by the cell's LEFT EDGE.

    The left edge, specifically -- not the midpoint, and not the width. These
    grids do not size a cell to its column: the "Select a product" dialog
    reports its Item No. cell as **790 px wide**, spanning essentially the whole
    row, while still starting exactly at the Item No. column and carrying that
    column's text. Judging by midpoint puts such a cell under a column three
    places to its right; treating width as evidence of "row decoration"
    discards it outright. An earlier version of this function did the latter
    and silently dropped every SKU, so existing products looked missing and got
    created a second time.

    Where a cell starts is the one thing that reliably identifies its column,
    so the rule is simply: the last column that begins at or before the cell.
    Genuine decoration (the full-width blank Text these grids also emit) is
    excluded by its empty text before it ever reaches here.
    """
    if not bounds:
        return None
    left = cell_rect[0]
    chosen: int | None = None
    for i, (b_left, _b_right) in enumerate(bounds):
        if b_left <= left + 2:  # 2px tolerance for sub-pixel/border offsets
            chosen = i
        else:
            break
    return chosen


def read_table_rows(grid, columns: list[str]) -> list[dict[str, str]]:
    """Read every row of an accessible grid, returning one dict per row keyed by
    `columns` in left-to-right order, PLUS a `"_row"` key holding the row's own
    pywinauto wrapper. Generic on purpose -- it doesn't assume a specific
    pywinauto row-wrapper API, just that each row is a container whose
    text-bearing descendants line up left-to-right with the declared columns.

    The `_row` wrapper matters: reading a row's text is not the same as
    selecting it. Confirmed live -- a real bug, not a hypothetical -- that
    clicking a dialog's OK button after only reading row text (never clicking
    the row itself to select it first) silently does nothing: no item gets
    added, no error is raised, the dialog just closes. `click_row()` below
    uses this wrapper to select the row before OK is invoked.

    Cells are assigned to columns by **horizontal position against the grid's
    own header rectangles** whenever the grid exposes headers, and only fall
    back to left-to-right sequence when it doesn't. Sequence alone is wrong,
    and wrongly in the most damaging way: blank cells produce no Text element
    at all, so a row with an empty First Name and Last Name yields two fewer
    values and every remaining value slides two columns left. Live symptom --
    a saved contact reported as `Company='Amsterdam', First Name='Vanguard
    Systems B.V.', ZIP=''`, so the exact-match test could never match a row
    that was in fact perfectly correct, and the flow created a duplicate.
    Position is the only thing that survives a missing cell.
    """
    header_bounds = _header_bounds(grid)
    rows: list[dict[str, str]] = []
    row_nodes = []
    for ct in ("DataItem", "ListItem", "Custom"):
        try:
            row_nodes = grid.descendants(control_type=ct)
        except Exception:
            row_nodes = []
        if row_nodes:
            break

    for row in row_nodes:
        cells: list[tuple[int, int, str]] = []  # (left, right, text)
        try:
            texts_source = row.descendants(control_type="Text") or [row]
        except Exception:
            texts_source = [row]
        for c in texts_source:
            try:
                text = c.window_text() or ""
                r = c.rectangle()
            except Exception:
                continue
            if text.strip():
                cells.append((r.left, r.right, text))
        if not cells:
            continue
        cells.sort(key=lambda c: c[0])

        row_dict: dict[str, str] = {col: "" for col in columns}
        if header_bounds:
            # Position-based: immune to a blank cell simply not existing.
            for left, right, text in cells:
                idx = _column_for((left, right), header_bounds)
                if idx is not None and idx < len(columns):
                    row_dict[columns[idx]] = text
        else:
            # No headers to align against -- sequential, and accept that a
            # missing cell shifts everything after it.
            for i, col in enumerate(columns):
                row_dict[col] = cells[i][2] if i < len(cells) else ""

        row_dict["_row"] = row
        rows.append(row_dict)
    return rows


def header_columns(grid) -> list[str]:
    """The grid's own column captions, left to right.

    Hardcoding a column list is how the same bug appeared three times: the
    VATs list was read as ["Name","Value","Standard"] when it is really
    Standard/Name/Description/Value, and the Payments list was read as
    ["Name"] when Standard comes first -- so a blank cell was compared against
    a payment method name, never matched, and a duplicate was created on every
    run. Asking the grid what its columns are removes the guess entirely.

    Returns [] if the grid exposes no headers, so callers can fall back to an
    explicit list rather than crash.
    """
    try:
        headers = grid.descendants(control_type="HeaderItem")
    except Exception:
        return []
    captions: list[tuple[int, str]] = []
    for h in headers:
        try:
            captions.append((h.rectangle().left, h.window_text() or ""))
        except Exception:
            continue
    captions.sort(key=lambda c: c[0])
    return [text for _, text in captions]


def click_row(row_dict: dict) -> None:
    """Select a row returned by read_table_rows() -- click_input() on the row
    wrapper itself, not just reading its text. Required before OK/select
    actions in a dialog; see read_table_rows()'s docstring for why this is a
    separate, necessary step, confirmed live the hard way."""
    wrapper = row_dict.get("_row")
    if wrapper is None:
        raise ControlNotFound("row dict has no '_row' wrapper to click -- was it from read_table_rows()?")
    wrapper.click_input()


def row_count(grid) -> int:
    for ct in ("DataItem", "ListItem", "Custom"):
        try:
            found = grid.descendants(control_type=ct)
        except Exception:
            found = []
        if found:
            return len(found)
    return 0