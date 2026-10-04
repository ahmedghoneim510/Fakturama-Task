"""Local OCR extraction: order image -> SourceOrder-shaped dict, no LLM, no network.

The third extraction provider, next to Claude and Gemini (`--provider ocr`). It
returns the same contract they do -- a dict that goes through the same
parse_extraction() (tier 1) and reconcile() (tier 2) gates -- so nothing
downstream knows or cares which provider read the image.

Two stages, kept apart on purpose:

  1. RapidOcrEngine turns pixels into OcrBoxes: a text line, its rectangle and
     the recognizer's score. This is the only part that needs the OCR package.
  2. parse_order() turns boxes into fields by LAYOUT, the same idea as
     uia/locator.py: find a label ("EXTERNAL REFERENCE"), take the nearest box
     below it; find the items table's header captions, assign every cell to a
     column by position. It is pure Python over a list of boxes, so it is unit
     tested without the OCR package installed.

What OCR is and isn't good for here. An LLM reads an unseen layout by
understanding it; this parser only knows the labels in LABELS and HEADERS below,
so a document from a different template fails loudly (ExtractionError naming
the missing label) rather than being half-read. In exchange it is free,
offline, deterministic (the same image always gives the same answer) and keeps
the customer's data on the machine.

Same rule as the rest of the pipeline: never guess. A required field that cannot
be found or parsed is an ExtractionError. A cell the page pass missed is re-read
once from a crop of exactly that cell; if it is still empty, the run stops.
Numbers are never "corrected" (no O->0 swaps) -- a wrong read must fail
reconcile(), not be repaired into a plausible value.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fic.errors import ExtractionError

ENGINE_NAME = "rapidocr-onnxruntime (PP-OCRv4)"

# Detection settings, tuned against data/input/order_001.png. With the library
# defaults (box_thresh=0.5, unclip_ratio=1.6) the detector draws boxes tight
# enough that the recognizer drops most spaces ('NorthstarOfficeGmbH',
# '10553Berlin', '+493055501420') and misses single-digit cells entirely (both
# Qty values). Wider, more permissive boxes fixed all of it but one Qty cell,
# which the per-cell re-read below recovers.
BOX_THRESH = 0.3
UNCLIP_RATIO = 2.0
CELL_PAD_PX = 6

# Same threshold reconcile() applies to SourceOrder.confidence.
LOW_SCORE = 0.80


@dataclass(frozen=True)
class OcrBox:
    text: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom
    score: float

    @property
    def cx(self) -> float:
        return (self.rect[0] + self.rect[2]) / 2

    @property
    def cy(self) -> float:
        return (self.rect[1] + self.rect[3]) / 2

    @property
    def h(self) -> int:
        return self.rect[3] - self.rect[1]


# Re-reads one region of the page: rect in page pixels -> box, or None if blank.
Reread = Callable[[tuple[int, int, int, int]], OcrBox | None]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class RapidOcrEngine:
    """RapidOCR: PaddleOCR's PP-OCRv4 models on onnxruntime. Chosen because it is
    pip-only (no system binary to install on Windows, unlike Tesseract) and ships
    its models inside the wheel, so the first run needs no download either."""

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # lazy: optional dependency
        except ImportError as exc:
            raise ExtractionError(
                "the OCR provider needs the optional 'ocr' extra",
                provider="ocr",
                hint="uv sync --extra ocr   (or: pip install -e '.[ocr]'); needs Python < 3.13",
            ) from exc
        self._engine = RapidOCR()

    def read_page(self, image) -> list[OcrBox]:
        result, _ = self._engine(image, box_thresh=BOX_THRESH, unclip_ratio=UNCLIP_RATIO)
        boxes = []
        for quad, text, score in result or []:
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            rect = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
            box = OcrBox(text.strip(), rect, float(score))
            if box.text:
                boxes.append(self._respaced(image, box))
        return boxes

    def _respaced(self, image, box: OcrBox) -> OcrBox:
        """Second read of one detected line, used ONLY to restore lost spaces.
        On the sample, page-level recognition glued 'Northstar OfficeWarehouse'
        while a crop of the same box read 'Northstar Office Warehouse'. Crops
        also sometimes drop letters, so see respace(): the re-read may move
        spaces and nothing else."""
        if " " not in box.text and len(box.text) < 4:
            return box
        alt = self.read_region(image, box.rect)
        if alt is None:
            return box
        text = respace(box.text, alt.text)
        if text == box.text:
            return box
        return OcrBox(text, box.rect, min(box.score, alt.score))

    def read_region(self, image, rect: tuple[int, int, int, int]) -> OcrBox | None:
        """Recognition only, on a crop of `rect` -- no detection step, so a cell
        the page-level detector overlooked (a lone '3') still gets read."""
        left, top, right, bottom = rect
        crop = image.crop(
            (
                max(0, left - CELL_PAD_PX),
                max(0, top - CELL_PAD_PX),
                min(image.width, right + CELL_PAD_PX),
                min(image.height, bottom + CELL_PAD_PX),
            )
        )
        result, _ = self._engine(crop, use_det=False, use_cls=False)
        if not result:
            return None
        text, score = result[0][0].strip(), float(result[0][1])
        return OcrBox(text, rect, score) if text else None


def read_order(image_path: Path) -> tuple[dict, list[str]]:
    """OCR `image_path` and parse it. Returns (SourceOrder-shaped dict, notes)."""
    from PIL import Image

    engine = RapidOcrEngine()
    with Image.open(image_path) as raw:
        image = _flatten(raw)
    boxes = engine.read_page(image)
    if not boxes:
        raise ExtractionError("OCR found no text in the image", provider="ocr", image=str(image_path))
    return parse_order(boxes, reread=lambda rect: engine.read_region(image, rect))


def _flatten(raw):
    """Image -> RGB on a white background. Done here, not left to RapidOCR:
    its own loader turns an RGBA image into its colour NEGATIVE (white text on
    black -- see LoadImage.cvt_four_to_three), and the sample order is an RGBA
    PNG. A plain convert('RGB') would instead drop alpha onto whatever colour
    the transparent pixels happen to hold."""
    from PIL import Image

    if raw.mode in ("RGBA", "LA") or (raw.mode == "P" and "transparency" in raw.info):
        rgba = raw.convert("RGBA")
        white = Image.new("RGB", rgba.size, "white")
        white.paste(rgba, mask=rgba.getchannel("A"))
        return white
    return raw.convert("RGB")


def respace(page_text: str, reread_text: str) -> str:
    """Take the re-read's word spacing only if it has the SAME characters and more
    word breaks; otherwise keep the page read. Characters are never changed --
    'EUR570.00' may become 'EUR 570.00', 'Unitnet' may not become 'Unit Ret'."""
    candidate = " ".join(reread_text.split())
    if re.sub(r"\s", "", candidate) != re.sub(r"\s", "", page_text):
        return page_text
    return candidate if candidate.count(" ") > page_text.count(" ") else page_text


# --------------------------------------------------------------------------- #
# Layout parser (pure -- no OCR package needed)
# --------------------------------------------------------------------------- #
# Labels of the order template, with the aliases a second template could add.
# Matching ignores case, spaces and punctuation: OCR reads small capitals labels
# with or without their spaces ('ORDER DATE' and 'ORDERDATE' both happen).
LABELS: dict[str, tuple[str, ...]] = {
    "external_reference": ("External Reference", "Order No", "Order Number"),
    "order_date": ("Order Date",),
    "customer_id": ("Customer ID",),
    "currency": ("Currency",),
    "company": ("Company",),
    "alias": ("Customer Alias",),
    "contact": ("Contact Name", "Contact"),
    "email": ("Email", "E-Mail"),
    "phone": ("Phone", "Telephone"),
    "billing": ("Billing Address",),
    "delivery": ("Delivery Address", "Shipping Address"),
    "payment_section": ("Payment",),
    "payment_method": ("Payment Method",),
    "paid_status": ("Paid Status", "Payment Status"),
    "payment_date": ("Payment Date",),
    "order_discount": ("Order Discount",),
    "shipping": ("Shipping",),
    "net_total": ("Net Total",),
    "vat_total": ("VAT Total",),
    "gross_total": ("Gross Total",),
}

# Items-table header captions -> column key. The '(EUR)' suffix is dropped by
# _norm_header, and OCR often reads 'Unit net' as 'Unitnet'.
HEADERS: dict[str, str] = {
    "#": "pos",
    "pos": "pos",
    "sku": "sku",
    "itemno": "sku",
    "description": "description",
    "qty": "qty",
    "quantity": "qty",
    "unit": "unit",
    "unitnet": "unit_net",
    "unitprice": "unit_net",
    "disc": "disc",
    "discount": "disc",
    "vat": "vat",
    "linenet": "line_net",
    "linetotal": "line_net",
}
REQUIRED_COLUMNS = ("sku", "description", "qty", "unit_net", "vat", "line_net")
# Columns whose cell may not be blank when the column exists. Blank Unit is fine.
REQUIRED_CELLS = ("qty", "unit_net", "vat", "line_net", "disc")


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.casefold())


def _norm_header(s: str) -> str:
    if s.strip() == "#":
        return "#"
    n = _norm(s)
    return n[:-3] if n.endswith("eur") and n != "eur" else n


def _h_overlap(a: tuple, b: tuple) -> float:
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    return overlap / (min(a[2] - a[0], b[2] - b[0]) or 1)


class _Page:
    def __init__(self, boxes: list[OcrBox]) -> None:
        self.boxes = boxes
        self.scores: dict[str, float] = {}  # field path -> OCR score of what was read

    def label(self, key: str, *, required: bool = True) -> OcrBox | None:
        wanted = {_norm(a) for a in LABELS[key]}
        hits = [b for b in self.boxes if _norm(b.text) in wanted]
        if not hits:
            if required:
                raise ExtractionError(
                    f"OCR: label {LABELS[key][0]!r} not found -- is this the expected order template?",
                    provider="ocr",
                    field=key,
                )
            return None
        if len(hits) > 1:
            # Two identical labels mean the layout is not the one this parser
            # knows. Picking one would be a guess, same as locator.py refuses to.
            raise ExtractionError(
                f"OCR: label {LABELS[key][0]!r} appears {len(hits)} times -- ambiguous layout",
                provider="ocr",
                field=key,
                candidates=[b.rect for b in hits],
            )
        return hits[0]

    def value_below(self, key: str, field: str, *, required: bool = True) -> str | None:
        """The nearest box under label `key` that overlaps it horizontally."""
        anchor = self.label(key, required=required)
        if anchor is None:
            return None
        label_texts = {_norm(a) for aliases in LABELS.values() for a in aliases}
        below = [
            b
            for b in self.boxes
            if b is not anchor
            and b.rect[1] >= anchor.cy
            and b.rect[1] - anchor.rect[3] <= 3 * anchor.h
            and _h_overlap(b.rect, anchor.rect) >= 0.3
            and _norm(b.text) not in label_texts
        ]
        if not below:
            if required:
                raise ExtractionError(
                    f"OCR: no value found under {LABELS[key][0]!r}", provider="ocr", field=field
                )
            return None
        hit = min(below, key=lambda b: b.rect[1])
        self.scores[field] = hit.score
        return hit.text

    def block_below(self, key: str, stop_y: int) -> list[OcrBox]:
        """Lines left-aligned under label `key`, above `stop_y`, top to bottom --
        an address block. Left alignment is what separates the billing column
        from the delivery column printed beside it."""
        anchor = self.label(key)
        lines = [
            b
            for b in self.boxes
            if b.rect[1] > anchor.rect[3]
            and b.rect[3] < stop_y
            and abs(b.rect[0] - anchor.rect[0]) <= 2 * anchor.h
        ]
        return sorted(lines, key=lambda b: b.rect[1])


def _money(text: str, field: str) -> str:
    """'EUR 1.234,50' / '$1,234.50' / '570.00' -> '1234.50'. Raises rather than
    guessing when the text isn't a number."""
    s = re.sub(r"(?i)eur|usd|chf|[€$£\s]", "", text)
    if "," in s and "." in s:
        # The LAST separator is the decimal one: 1.234,50 vs 1,234.50.
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.fullmatch(r"-?\d+,\d{1,2}", s) else s.replace(",", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        raise ExtractionError(
            f"OCR: {field} is not a number: {text!r}", provider="ocr", field=field, raw_value=text
        )
    return s


def _percent(text: str, field: str) -> str:
    return _money(text.replace("%", ""), field)


def _date(text: str | None, field: str) -> str | None:
    """ISO or DD.MM.YYYY only. A slashed date is ambiguous (07/08 is July in the
    US and August in Germany) and is refused rather than read one way."""
    if text is None:
        return None
    s = text.strip()
    if re.fullmatch(r"[-–—]+|n/?a", s, re.IGNORECASE):
        return None  # printed placeholder for "no date"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s):
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    raise ExtractionError(
        f"OCR: {field} is not an unambiguous date: {text!r}", provider="ocr", field=field, raw_value=text
    )


def _split_zip_city(text: str) -> tuple[str, str]:
    """'10117 Berlin' -> ('10117', 'Berlin'). Also accepts the glued form OCR can
    produce ('10553Berlin'): digits followed by letters splits only one way."""
    if m := re.fullmatch(r"(\S*\d\S*)\s+(.+)", text.strip()):
        return m[1], m[2]
    if m := re.fullmatch(r"(\d+)(\D.*)", text.strip()):
        return m[1], m[2].strip()
    raise ExtractionError(f"OCR: cannot split ZIP and city in {text!r}", provider="ocr", raw_value=text)


def _address(lines: list[OcrBox], which: str) -> dict:
    """name / street (one or more lines) / 'ZIP City' / country."""
    if len(lines) < 4:
        raise ExtractionError(
            f"OCR: {which} address has {len(lines)} lines, expected at least 4 "
            "(name, street, ZIP + city, country)",
            provider="ocr",
            lines=[b.text for b in lines],
        )
    zip_code, city = _split_zip_city(lines[-2].text)
    return {
        "name": lines[0].text,
        "street": " ".join(b.text for b in lines[1:-2]),
        "zip": zip_code,
        "city": city,
        "country": lines[-1].text,
    }


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    """'Marta Klein' -> ('Marta', 'Klein'); 'Anna Maria Schmidt' -> ('Anna Maria',
    'Schmidt'). A heuristic -- the LLM providers split by understanding the name,
    this one by position, which is wrong for 'van der Berg'-style surnames."""
    if not full:
        return None, None
    parts = full.split()
    if len(parts) == 1:
        return None, parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _items(page: _Page, body_end: int, reread: Reread | None) -> list[dict]:
    header_boxes = [(HEADERS[_norm_header(b.text)], b) for b in page.boxes if _norm_header(b.text) in HEADERS]
    sku_header = next((b for key, b in header_boxes if key == "sku"), None)
    if sku_header is None:
        raise ExtractionError("OCR: items table header 'SKU' not found", provider="ocr")
    # Keep only captions on the SKU header's own row -- 'Unit' or 'VAT' can also
    # appear elsewhere on the page.
    row_band = 1.5 * sku_header.h
    columns: dict[str, OcrBox] = {}
    for key, b in header_boxes:
        if abs(b.cy - sku_header.cy) <= row_band and key not in columns:
            columns[key] = b
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise ExtractionError(f"OCR: items table columns not found: {missing}", provider="ocr")

    # Column extents: halfway to each neighbour's centre. Cells are assigned by
    # their centre, which tolerates both centred and left-aligned cell text.
    order = sorted(columns.items(), key=lambda kv: kv[1].cx)
    bounds: dict[str, tuple[float, float]] = {}
    for i, (key, b) in enumerate(order):
        left = (order[i - 1][1].cx + b.cx) / 2 if i else b.rect[0] - b.h
        right = (b.cx + order[i + 1][1].cx) / 2 if i + 1 < len(order) else b.rect[2] + b.h
        bounds[key] = (left, right)

    def column_of(box: OcrBox) -> str | None:
        return next((k for k, (lo, hi) in bounds.items() if lo <= box.cx < hi), None)

    header_bottom = max(b.rect[3] for b in columns.values())
    body = [b for b in page.boxes if b.rect[1] > header_bottom and b.rect[3] < body_end]

    # A row exists where an SKU was read. A row whose SKU was missed is not
    # invented -- its absence makes the line sum disagree with Net Total, and
    # reconcile() halts.
    skus = sorted((b for b in body if column_of(b) == "sku"), key=lambda b: b.cy)
    if not skus:
        raise ExtractionError("OCR: no item rows found under the items header", provider="ocr")

    items = []
    for pos, sku in enumerate(skus, start=1):
        cells: dict[str, list[OcrBox]] = {}
        for b in body:
            if b is sku or abs(b.cy - sku.cy) > 0.8 * max(sku.h, b.h):
                continue
            col = column_of(b)
            if col:
                cells.setdefault(col, []).append(b)

        def cell(col: str, field: str) -> str:
            found = sorted(cells.get(col, []), key=lambda b: b.rect[0])
            if found:
                page.scores[field] = min(b.score for b in found)
                return " ".join(b.text for b in found)
            if col not in bounds:
                return ""
            if col in REQUIRED_CELLS and reread is not None:
                # The page pass missed this cell -- read exactly that rectangle.
                hdr = columns[col]
                lo, hi = bounds[col]
                rect = (
                    int(max(lo, hdr.rect[0] - (hdr.rect[2] - hdr.rect[0]) / 2)),
                    sku.rect[1],
                    int(min(hi, hdr.rect[2] + (hdr.rect[2] - hdr.rect[0]) / 2)),
                    sku.rect[3],
                )
                hit = reread(rect)
                if hit is not None:
                    page.scores[field] = hit.score
                    return hit.text
            if col in REQUIRED_CELLS:
                raise ExtractionError(
                    f"OCR: item row {pos} ({sku.text}) has no readable {col!r} cell",
                    provider="ocr",
                    field=field,
                )
            return ""

        p = f"items[{pos - 1}]"
        page.scores[f"{p}.sku"] = sku.score
        disc = cell("disc", f"{p}.discount_pct")
        items.append(
            {
                "position": pos,
                "sku": sku.text,
                "description": cell("description", f"{p}.description"),
                "quantity": _money(cell("qty", f"{p}.quantity"), f"{p}.quantity"),
                "unit": cell("unit", f"{p}.unit") or None,
                "unit_net_price": _money(cell("unit_net", f"{p}.unit_net_price"), f"{p}.unit_net_price"),
                "discount_pct": _percent(disc, f"{p}.discount_pct") if disc else "0",
                "vat_pct": _percent(cell("vat", f"{p}.vat_pct"), f"{p}.vat_pct"),
                "line_net_total": _money(cell("line_net", f"{p}.line_net_total"), f"{p}.line_net_total"),
            }
        )
    return items


# Fields reconcile() cannot check arithmetically. Their OCR scores go into
# SourceOrder.confidence, so a shaky read of one halts the run for review. Money
# and percentages are deliberately NOT in this set: each sits in an equation
# reconcile() checks (qty x price x discount = line, lines = net, ...), and a
# misread there fails that equation -- stronger evidence than a recognizer score.
_CONFIDENCE_FIELDS = re.compile(
    r"^(external_reference|order_date|payment\.(method|paid_status|payment_date)"
    r"|debtor\.company|items\[\d+\]\.sku)$"
)


def parse_order(boxes: list[OcrBox], *, reread: Reread | None = None) -> tuple[dict, list[str]]:
    """OCR boxes -> (SourceOrder-shaped dict, notes). Values stay strings; the
    pydantic model does the typing, exactly as for an LLM's output."""
    page = _Page(boxes)

    payment_label = page.label("payment_section")
    billing = _address(page.block_below("billing", payment_label.rect[1]), "billing")
    delivery_label = page.label("delivery", required=False)
    delivery = (
        _address(page.block_below("delivery", payment_label.rect[1]), "delivery")
        if delivery_label
        else None
    )
    first_name, last_name = _split_name(page.value_below("contact", "debtor.contact", required=False))

    paid_status = page.value_below("paid_status", "payment.paid_status")
    totals_top = page.label("net_total").rect[1]
    discount = page.value_below("order_discount", "order_discount_pct", required=False)
    shipping = page.value_below("shipping", "shipping_amount", required=False)

    data = {
        "external_reference": page.value_below("external_reference", "external_reference"),
        "order_date": _date(page.value_below("order_date", "order_date"), "order_date"),
        "currency": (page.value_below("currency", "currency", required=False) or "EUR").upper(),
        "order_discount_pct": _percent(discount, "order_discount_pct") if discount else "0",
        "shipping_amount": _money(shipping, "shipping_amount") if shipping else "0",
        "debtor": {
            "company": page.value_below("company", "debtor.company"),
            "first_name": first_name,
            "last_name": last_name,
            "alias": page.value_below("alias", "debtor.alias", required=False),
            "email": page.value_below("email", "debtor.email", required=False),
            "phone": page.value_below("phone", "debtor.phone", required=False),
            "customer_id_hint": page.value_below("customer_id", "debtor.customer_id_hint", required=False),
            "billing": billing,
            "delivery": delivery,
        },
        "payment": {
            "method": page.value_below("payment_method", "payment.method"),
            # Upper-cased only; anything but PAID/UNPAID is rejected by the model.
            "paid_status": paid_status.strip().upper(),
            "payment_date": _date(
                page.value_below("payment_date", "payment.payment_date", required=False),
                "payment.payment_date",
            ),
        },
        "items": _items(page, totals_top, reread),
        "net_total": _money(page.value_below("net_total", "net_total"), "net_total"),
        "vat_total": _money(page.value_below("vat_total", "vat_total"), "vat_total"),
        "gross_total": _money(page.value_below("gross_total", "gross_total"), "gross_total"),
    }
    data["confidence"] = {
        f: round(s, 3) for f, s in page.scores.items() if _CONFIDENCE_FIELDS.match(f)
    }
    notes = [
        f"OCR score {s:.2f} on {f} -- below {LOW_SCORE}, left to the arithmetic check"
        for f, s in page.scores.items()
        if s < LOW_SCORE and not _CONFIDENCE_FIELDS.match(f)
    ]
    return data, notes
