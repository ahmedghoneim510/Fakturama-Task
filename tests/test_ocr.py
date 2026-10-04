"""OCR provider tests.

The parser tests run on tests/fixtures/order_001_ocr_boxes.json -- the real
RapidOCR output for data/input/order_001.png, captured once -- so they need
neither the OCR package nor the image. Only test_live_ocr_matches_golden runs
the engine itself, and it skips when the `ocr` extra isn't installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fic import extraction, ocr
from fic.errors import ExtractionError, ManualReviewRequired
from fic.models import SourceOrder

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "data" / "golden" / "order_001.json"
SAMPLE_IMAGE = ROOT / "data" / "input" / "order_001.png"
BOXES = ROOT / "tests" / "fixtures" / "order_001_ocr_boxes.json"


def _boxes() -> list[ocr.OcrBox]:
    return [ocr.OcrBox(b["text"], tuple(b["rect"]), b["score"]) for b in json.loads(BOXES.read_text("utf-8"))]


def _golden() -> dict:
    return SourceOrder.model_validate_json(GOLDEN.read_text("utf-8")).model_dump(exclude={"confidence"})


def _parse(boxes, reread=None) -> SourceOrder:
    data, _ = ocr.parse_order(boxes, reread=reread)
    return extraction.parse_extraction(data, source="ocr", model="test")


# --------------------------------------------------------------------------- #
# Parser on the real OCR output
# --------------------------------------------------------------------------- #
def test_parser_reproduces_golden_from_real_ocr_boxes():
    order = _parse(_boxes())
    assert order.model_dump(exclude={"confidence"}) == _golden()
    extraction.reconcile(order)  # passes tier 2, confidence scores included


def test_confidence_covers_only_fields_arithmetic_cannot_check():
    data, _ = ocr.parse_order(_boxes())
    assert set(data["confidence"]) == {
        "external_reference",
        "order_date",
        "debtor.company",
        "payment.method",
        "payment.paid_status",
        "payment.payment_date",
        "items[0].sku",
        "items[1].sku",
    }


def _without(boxes, text, near_y):
    return [b for b in boxes if not (b.text == text and abs(b.cy - near_y) < 20)]


def test_missed_cell_is_reread_from_its_own_crop():
    boxes = _without(_boxes(), "3", 1580)  # row 2's Qty -- missed by the page pass on some settings
    calls = []

    def reread(rect):
        calls.append(rect)
        return ocr.OcrBox("3", rect, 0.72)

    order = _parse(boxes, reread)
    assert order.items[1].quantity == 3
    assert len(calls) == 1
    left, top, right, bottom = calls[0]
    assert 700 < left < 760 < right < 800  # inside the Qty column, not Unit
    assert top <= 1580 <= bottom  # on row 2


def test_missed_cell_that_stays_blank_halts():
    boxes = _without(_boxes(), "3", 1580)
    with pytest.raises(ExtractionError, match="no readable 'qty' cell"):
        _parse(boxes, reread=lambda rect: None)


def test_missed_sku_row_is_not_invented_and_reconcile_halts():
    order = _parse(_without(_boxes(), "MAT-DESK-02", 1580))
    assert len(order.items) == 1
    with pytest.raises(ManualReviewRequired):
        extraction.reconcile(order)


def test_missing_label_names_itself():
    boxes = [b for b in _boxes() if b.text != "NET TOTAL"]
    with pytest.raises(ExtractionError, match="Net Total"):
        ocr.parse_order(boxes)


def test_duplicate_label_is_refused_not_guessed():
    boxes = _boxes()
    company = next(b for b in boxes if b.text == "COMPANY")
    boxes.append(ocr.OcrBox("COMPANY", (900, 1700, 990, 1720), 0.99))
    with pytest.raises(ExtractionError, match="appears 2 times"):
        ocr.parse_order(boxes)
    assert company in boxes


def test_glued_zip_city_still_splits():
    boxes = [
        ocr.OcrBox("10553Berlin", b.rect, b.score) if b.text == "10553 Berlin" else b for b in _boxes()
    ]
    order = _parse(boxes)
    assert (order.debtor.delivery.zip, order.debtor.delivery.city) == ("10553", "Berlin")


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("570.00", "570.00"),
        ("EUR570.00", "570.00"),
        ("EUR 1.234,50", "1234.50"),
        ("$1,234.50", "1234.50"),
        ("12,5", "12.5"),
        ("1,234", "1234"),
    ],
)
def test_money(raw, expected):
    assert ocr._money(raw, "f") == expected


@pytest.mark.parametrize("raw", ["57O.00", "abc", ""])
def test_money_refuses_non_numbers_instead_of_correcting(raw):
    with pytest.raises(ExtractionError):
        ocr._money(raw, "f")


def test_dates():
    assert ocr._date("2026-07-14", "f") == "2026-07-14"
    assert ocr._date("14.07.2026", "f") == "2026-07-14"
    assert ocr._date("-", "f") is None
    with pytest.raises(ExtractionError, match="unambiguous"):
        ocr._date("07/08/2026", "f")  # July or August depending on locale


def test_respace_moves_spaces_never_characters():
    fixed = "Northstar Office Warehouse"
    assert ocr.respace("Northstar OfficeWarehouse", fixed) == fixed
    assert ocr.respace("EUR570.00", " EUR 570.00 ") == "EUR 570.00"
    assert ocr.respace("Unitnet", "Unit Ret") == "Unitnet"  # a letter changed
    assert ocr.respace("Beusselstrasse 44", "Beussestrasse 4") == "Beusselstrasse 44"  # letters dropped
    assert ocr.respace("Office Warehouse", "OfficeWarehouse") == "Office Warehouse"  # fewer spaces


def test_split_name():
    assert ocr._split_name("Marta Klein") == ("Marta", "Klein")
    assert ocr._split_name("Anna Maria Schmidt") == ("Anna Maria", "Schmidt")
    assert ocr._split_name("Klein") == (None, "Klein")
    assert ocr._split_name(None) == (None, None)


# --------------------------------------------------------------------------- #
# Switching providers
# --------------------------------------------------------------------------- #
def _fake_read_order(monkeypatch, data=None):
    calls = []

    def fake(path):
        calls.append(path)
        return (data or ocr.parse_order(_boxes())[0]), []

    monkeypatch.setattr(ocr, "read_order", fake)
    return calls


def test_provider_ocr_dispatches_to_the_ocr_engine(monkeypatch):
    calls = _fake_read_order(monkeypatch)
    order = extraction.extract_and_reconcile(SAMPLE_IMAGE, provider="ocr")
    assert order.external_reference == "WEB-2026-0714-A17"
    assert calls == [SAMPLE_IMAGE]


def test_ocr_is_not_reread_when_incomplete(monkeypatch):
    """The completeness re-read is for stochastic LLM output; OCR would return
    the same thing twice."""
    data = ocr.parse_order(_boxes())[0]
    data["debtor"]["first_name"] = data["debtor"]["last_name"] = None
    calls = _fake_read_order(monkeypatch, data)
    monkeypatch.setattr(extraction, "EXTRACT_RETRIES", 1)
    extraction.extract_and_reconcile(SAMPLE_IMAGE, provider="ocr")
    assert len(calls) == 1


def test_unknown_provider_lists_the_valid_ones():
    with pytest.raises(ValueError, match="claude, gemini, ocr"):
        extraction._extract_once(SAMPLE_IMAGE, provider="tesseract", model=None)


def test_cross_check_llm_against_ocr(monkeypatch):
    _fake_read_order(monkeypatch)
    golden = SourceOrder.model_validate_json(GOLDEN.read_text("utf-8"))
    monkeypatch.setattr(extraction, "extract_with_gemini", lambda path, model=None: golden)
    order = extraction.self_consistency_check(SAMPLE_IMAGE, primary="gemini", secondary="ocr")
    assert order is golden


def test_cross_check_halts_when_ocr_and_llm_disagree(monkeypatch):
    _fake_read_order(monkeypatch)
    misread = SourceOrder.model_validate_json(GOLDEN.read_text("utf-8"))
    misread = misread.model_copy(update={"net_total": misread.net_total + 1})
    monkeypatch.setattr(extraction, "extract_with_gemini", lambda path, model=None: misread)
    monkeypatch.setattr(extraction, "reconcile", lambda order: None)
    with pytest.raises(ManualReviewRequired, match="gemini and ocr"):
        extraction.self_consistency_check(SAMPLE_IMAGE, primary="gemini", secondary="ocr")


def test_cross_check_rejects_the_same_provider_twice():
    with pytest.raises(ValueError, match="two different providers"):
        extraction.self_consistency_check(SAMPLE_IMAGE, primary="ocr", secondary="ocr")


def test_cli_rejects_unknown_provider_before_doing_anything():
    from typer.testing import CliRunner

    from fic.cli import app

    result = CliRunner().invoke(app, ["extract", str(SAMPLE_IMAGE), "--provider", "tesseract"])
    assert result.exit_code == 2
    flat = " ".join(result.output.replace("│", " ").split())  # undo rich's box wrapping
    assert "'tesseract' is not one of: claude, gemini, ocr" in flat


# --------------------------------------------------------------------------- #
# The real engine
# --------------------------------------------------------------------------- #
def test_live_ocr_matches_golden():
    """Runs RapidOCR on the sample image -- local and offline, ~4s."""
    pytest.importorskip("rapidocr_onnxruntime")
    order = extraction.extract_and_reconcile(SAMPLE_IMAGE, provider="ocr")
    assert order.model_dump(exclude={"confidence"}) == _golden()
