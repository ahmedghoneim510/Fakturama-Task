"""Tier-1 (pydantic) and tier-2 (reconcile) validation. Pure logic, no GUI, no
network -- this is the ~100%-branch-coverage tier per 03-Stack-...md S5."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from fic.extraction import reconcile
from fic.models import SourceItem, SourceOrder, SourcePayment, q2

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "golden" / "order_001.json"


def load_golden() -> SourceOrder:
    return SourceOrder.model_validate(json.loads(GOLDEN.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# Golden fixture
# --------------------------------------------------------------------------- #


def test_golden_fixture_parses():
    order = load_golden()
    assert order.external_reference == "WEB-2026-0714-A17"
    assert len(order.items) == 2


def test_golden_fixture_reconciles_cleanly():
    order = load_golden()
    reconcile(order)  # must not raise


def test_gross_price_canary_297_50_and_47_60():
    """The canary from 01-Full-Engineering-Spec.md: if this drifts, the
    rounding/formula wiring for the Product master price is wrong."""
    order = load_golden()
    chair, mat = order.items
    assert chair.product_master_gross() == Decimal("297.50")
    assert mat.product_master_gross() == Decimal("47.60")


def test_expected_line_net_matches_golden_totals():
    order = load_golden()
    chair, mat = order.items
    assert chair.expected_line_net() == Decimal("450.00")
    assert mat.expected_line_net() == Decimal("120.00")


def test_delivery_differs_from_billing_in_golden_fixture():
    """E15 / the brief's unstated else-branch: the sample fixture's delivery
    address genuinely differs from billing (different recipient name)."""
    order = load_golden()
    assert order.debtor.delivery_same_as_billing is False


# --------------------------------------------------------------------------- #
# Tier 1 -- pydantic validators
# --------------------------------------------------------------------------- #


def _item_kwargs(**overrides):
    base = dict(
        position=1,
        sku="SKU-1",
        description="A widget",
        quantity=Decimal("1"),
        unit_net_price=Decimal("10.00"),
        discount_pct=Decimal("0"),
        vat_pct=Decimal("19"),
        line_net_total=Decimal("10.00"),
    )
    base.update(overrides)
    return base


def test_paid_status_without_payment_date_rejected():
    with pytest.raises(ValidationError):
        SourcePayment(method="Bank Transfer", paid_status="PAID", payment_date=None)


def test_paid_status_with_payment_date_accepted():
    p = SourcePayment(method="Bank Transfer", paid_status="PAID", payment_date="2026-07-18")
    assert p.paid_status == "PAID"


def test_unpaid_without_payment_date_accepted():
    p = SourcePayment(method="Bank Transfer", paid_status="UNPAID")
    assert p.payment_date is None


def test_unrecognized_payment_status_rejected():
    with pytest.raises(ValidationError):
        SourcePayment(method="Bank Transfer", paid_status="PARTIALLY PAID", payment_date=None)


def test_zero_quantity_rejected():
    with pytest.raises(ValidationError):
        SourceItem(**_item_kwargs(quantity=Decimal("0")))


def test_negative_quantity_rejected():
    with pytest.raises(ValidationError):
        SourceItem(**_item_kwargs(quantity=Decimal("-1")))


def test_zero_price_line_accepted():
    # zero price is a legitimate promo/free line, not an error
    item = SourceItem(**_item_kwargs(unit_net_price=Decimal("0"), line_net_total=Decimal("0")))
    assert item.unit_net_price == 0


def test_negative_price_rejected():
    with pytest.raises(ValidationError):
        SourceItem(**_item_kwargs(unit_net_price=Decimal("-1")))


def test_blank_sku_rejected():
    with pytest.raises(ValidationError):
        SourceItem(**_item_kwargs(sku="   "))


def test_discount_pct_out_of_range_rejected():
    with pytest.raises(ValidationError):
        SourceItem(**_item_kwargs(discount_pct=Decimal("150")))


# --------------------------------------------------------------------------- #
# Tier 2 -- reconcile()
# --------------------------------------------------------------------------- #


def test_line_mismatch_beyond_tolerance_raises():
    from fic.errors import ManualReviewRequired

    order = load_golden()
    order.items[0].line_net_total = Decimal("999.99")  # deliberately wrong
    with pytest.raises(ManualReviewRequired):
        reconcile(order)


def test_order_total_mismatch_beyond_tolerance_raises():
    from fic.errors import ManualReviewRequired

    order = load_golden()
    order.gross_total = Decimal("1.00")
    with pytest.raises(ManualReviewRequired):
        reconcile(order)


def test_rounding_within_tolerance_does_not_raise():
    order = load_golden()
    order.gross_total = order.gross_total + Decimal("0.01")  # a cent of legitimate rounding
    reconcile(order)  # must not raise


def test_q2_rounds_half_up():
    assert q2(Decimal("1.005")) == Decimal("1.01")
    assert q2(Decimal("33.33") * Decimal("1.19")) == Decimal("39.66")


# --------------------------------------------------------------------------- #
# VAT percentages must never be written in scientific notation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "pct,expected_value,expected_name",
    [
        ("19", "19", "VAT 19%"),
        ("20", "20", "VAT 20%"),   # str(Decimal('20').normalize()) == '2E+1'
        ("10", "10", "VAT 10%"),   # str(Decimal('10').normalize()) == '1E+1'
        ("30", "30", "VAT 30%"),
        ("7", "7", "VAT 7%"),
        ("2.5", "2.5", "VAT 2.5%"),
        ("0", "0", "VAT 0%"),
    ],
)
def test_vat_pct_str_never_uses_scientific_notation(pct, expected_value, expected_name):
    """Regression for a live bug: writing str(Decimal('20').normalize()) into
    Fakturama's TAX Rate Value field typed '2E+1', which it stored as 0 -- a
    tax rate named 'VAT 20%' that was actually worth 0%."""
    item = SourceItem(
        position=1, sku="X", description="x", quantity=Decimal("1"),
        unit_net_price=Decimal("100"), vat_pct=Decimal(pct),
        line_net_total=Decimal("100"),
    )
    assert item.vat_pct_str() == expected_value
    assert "E" not in item.vat_pct_str().upper()
    assert item.vat_name() == expected_name
