"""Item-grid strategy for the Order's Items table (brief S3.13-3.16).

FULLY RESOLVED, live-confirmed end to end. This was the single biggest open
question in the whole design (see docs/reference/), and both halves of it are
now answered:

READING: the grid is a real, fully accessible ListView (control_type "List"
containing "ListItem" rows, each with ordinary "Text" cell children in column
order: Pos./Qty./Item No./Picture/Name/Description/VAT/U.Price/Discount/Price).
Not canvas-drawn, not NatTable -- confirmed against Fakturama's own source
(github.com/hernad/fakturama, DocumentEditor.java): it's a standard JFace
`TableViewer` with a `DocumentItemEditingSupport` (`EditingSupport` subclass)
providing a `TextCellEditor` per numeric column.

WRITING: the activation pattern is TWO SEPARATE SINGLE CLICKS -- click the row
(selects it), then a second, separate click on the target cell -- matching
JFace's `MOUSE_CLICK_SELECTION` editor-activation event. This is NOT a genuine
double-click (`double_click_input()` sends a single WM_LBUTTONDBLCLK message,
which SWT/JFace's click-counting listener does not treat the same as two
discrete clicks) and NOT F2 (tried live, both on the cell and on the table
control after selecting the row -- neither activated an editor). Five OTHER
patterns were tried and ruled out before this one was found: single click
alone, F2 on cell, F2 on table, F2 on row, click+type-immediately, double-click
on the row itself. The winning pattern was found by checking Fakturama's real
source for its editing-support class, which confirmed a plain `TextCellEditor`
and ruled out anything canvas/NatTable-specific, narrowing the search space
from "guess at a custom widget's private protocol" to "find the right JFace
click sequence" -- then live-confirmed against Qty specifically: writing "3"
into a `Qty=1` line changed it to `Qty=3` AND correctly recalculated
`Price: 1.90 $ -> 5.70 $`, an independent cross-check that the write reached
Fakturama's real data model, not just a UI artifact.

The transient editor is a genuine `Edit`-type element, but it is NOT parented
under the row/List in the UIA tree -- it must be found by diffing the set of
`Edit`-type elements in a broader scope (the whole editor window) before and
after the two-click activation. `get_value()` (not `window_text()`, per this
project's established rule) is the correct way to read its live content.
"""
from __future__ import annotations

import re
import time

from fic.errors import ControlNotFound, VerificationFailed
from fic.uia import actions

# Live-confirmed column order (the leading Pos. column has no header text but
# is a real cell).
COLUMN_ORDER = [
    "Pos.", "Qty.", "Item No.", "Picture", "Name", "Description",
    "VAT", "U.Price", "Discount", "Price",
]


def probe_grid_accessibility(region) -> bool:
    """True if the grid exposes addressable row/cell children. Live-confirmed
    TRUE for this Fakturama build: rows are "ListItem" (a JFace TableViewer),
    not "DataItem"/canvas as originally guessed -- included here alongside
    the original guesses rather than replacing them, since a different
    Fakturama version could genuinely use one of the others."""
    try:
        for ct in ("ListItem", "DataItem", "Custom", "Edit"):
            if region.descendants(control_type=ct):
                return True
    except Exception:
        pass
    return False


def read_line_values(row) -> dict[str, str]:
    """Read every cell of a ListItem row, left to right, live-confirmed
    working. `row` is one element of `region.descendants(control_type=
    "ListItem")`."""
    cells = sorted(row.descendants(control_type="Text"), key=lambda c: c.rectangle().left)
    values = [c.window_text() or "" for c in cells]
    return {col: (values[i] if i < len(values) else "") for i, col in enumerate(COLUMN_ORDER)}


def numeric(text: str) -> str:
    """Strip currency/percent decoration ('5.70 $', '10 %') down to the
    numeric MAGNITUDE (sign dropped), for comparing a typed value against a
    displayed one. Two things this normalizes over, both confirmed live:
    exact string equality doesn't hold since Fakturama adds its own suffix
    and may reformat (trailing zeros); and Fakturama displays Discount as a
    NEGATIVE percentage ('-10 %' for a typed '10') as a display convention --
    a discount is a negative price adjustment -- not a sign error in the
    write. None of this project's grid fields are ever meaningfully negative
    (Qty/U.Price/Price are always positive; Discount is a 0-100 magnitude
    regardless of Fakturama's own display sign), so dropping the sign
    entirely is correct here, not just convenient."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return m.group(0).replace(",", ".").lstrip("-") if m else ""


def _find_new_edit(scope, before_rects: set[tuple[int, int, int, int]]):
    for e in scope.descendants(control_type="Edit"):
        try:
            r = e.rectangle()
            key = (r.left, r.top, r.right, r.bottom)
        except Exception:
            continue
        if key not in before_rects:
            return e
    return None


def _snapshot_edit_rects(scope) -> set[tuple[int, int, int, int]]:
    rects = set()
    for e in scope.descendants(control_type="Edit"):
        try:
            r = e.rectangle()
            rects.add((r.left, r.top, r.right, r.bottom))
        except Exception:
            continue
    return rects


def set_cell_via_children(scope, row, column: str, value: str) -> None:
    """Write `value` into `column` of the given ListItem `row`. `scope` is
    the broader container (the Order editor / main window) to search for the
    transient Edit widget in -- it is NOT a descendant of `row` or of the
    grid's List control, confirmed live, so a narrower search scope would
    never find it.

    Activation: click the row, then (a separate, discrete) click on the
    target cell -- see module docstring for why this exact sequence and not
    the more obvious double-click/F2 alternatives. Commits with Enter, then
    reads back from the row's own cell text (post-commit; the transient
    editor is gone by then) using numeric comparison (see _numeric()) since
    Fakturama redisplays the value with its own formatting/suffix.
    """
    if column not in COLUMN_ORDER:
        raise ControlNotFound(f"unknown grid column {column!r}, expected one of {COLUMN_ORDER}")
    col_idx = COLUMN_ORDER.index(column)

    cells = sorted(row.descendants(control_type="Text"), key=lambda c: c.rectangle().left)
    if col_idx >= len(cells):
        raise ControlNotFound(f"row has no cell at column {column!r} (index {col_idx})")
    target_cell = cells[col_idx]

    before = _snapshot_edit_rects(scope)
    row.click_input()
    time.sleep(0.15)
    target_cell.click_input()
    time.sleep(0.3)

    editor_ctrl = _find_new_edit(scope, before)
    if editor_ctrl is None:
        raise VerificationFailed(
            f"clicking column {column!r} did not activate an editable widget -- "
            "the row-then-cell click sequence that normally triggers it "
            "(see grid.py module docstring) had no effect this time",
        )

    editor_ctrl.type_keys("^a", pause=0.05)
    # escape_keys: type_keys() parses SendKeys syntax, so a literal '%' / '+'
    # / '(' in a value would be swallowed as a modifier -- see
    # actions.escape_keys. Grid values are numeric today, but a percent-
    # suffixed or signed value reaching here must not silently mistype.
    editor_ctrl.type_keys(actions.escape_keys(str(value)), pause=0.05)
    editor_ctrl.type_keys("{ENTER}", pause=0.1)
    time.sleep(0.3)

    cells_after = sorted(row.descendants(control_type="Text"), key=lambda c: c.rectangle().left)
    actual = cells_after[col_idx].window_text() if col_idx < len(cells_after) else ""
    if numeric(actual) != numeric(str(value)):
        raise VerificationFailed(
            f"grid cell {column!r} did not read back as written", wrote=value, read=actual
        )


# --------------------------------------------------------------------------- #
# Canvas-path fallback -- NOT this build's actual problem (the grid is
# accessible here, see above), kept only for a hypothetical Fakturama version
# where the grid genuinely is canvas-drawn. Still unimplemented.
# --------------------------------------------------------------------------- #


def learn_column_order(get_focused_editor, header_rects: dict[str, tuple[int, int, int, int]]):
    raise NotImplementedError(
        "canvas-path column learner -- not needed for this build (the grid is "
        "accessible; see set_cell_via_children()), kept for a hypothetical "
        "canvas-drawn Fakturama version."
    )


def set_cell_via_keyboard(focused_editor_ctrl, value: str) -> None:
    focused_editor_ctrl.type_keys("^a{DELETE}", pause=0.02)
    if value:
        focused_editor_ctrl.type_keys(value, with_spaces=True, pause=0.01)
    written = (focused_editor_ctrl.window_text() or "").strip()
    if written != value.strip():
        raise VerificationFailed(
            "grid cell did not read back correctly before commit", wrote=value, read=written
        )
    focused_editor_ctrl.type_keys("{TAB}", pause=0.02)
