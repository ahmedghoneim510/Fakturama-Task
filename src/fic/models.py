"""Domain model for the extracted order (tier-1 validation).

Money is Decimal end to end, never float, and serialized as strings. Every validator
here encodes a corner case from the design docs so that constructing an invalid
SourceOrder is impossible rather than merely checked later:

  - E5  blank SKU / missing required field -> rejected
  - E7  zero/negative quantity -> rejected (zero *price* is legitimate, not rejected)
  - E8  payment status other than PAID/UNPAID -> rejected
  - E9  PAID with no payment_date -> rejected (the brief forbids inventing a date but
        requires one when PAID; that's a contradiction for such an input, not
        something to silently resolve one way)

What is deliberately NOT a validator here: company name format, address completeness
beyond the required fields, alias presence. Those need a documented policy (normalize-
for-comparison only, derive a slug, leave blank) rather than a reject/accept gate --
see 01-Full-Engineering-Spec.md S4.1/S5.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def q2(x: Decimal) -> Decimal:
    """Round-half-up to 2 decimal places -- the one rounding rule used everywhere
    money is computed, so every computed value is reproducible and comparable."""
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SourceAddress(BaseModel):
    name: str
    street: str
    zip: str
    city: str
    country: str

    @field_validator("name", "street", "zip", "city", "country")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("address field must not be blank")
        return v


class SourceDebtor(BaseModel):
    company: str
    first_name: str | None = None
    last_name: str | None = None
    alias: str | None = None
    email: str | None = None
    phone: str | None = None
    customer_id_hint: str | None = None  # from image; NEVER written back (S2.6)
    billing: SourceAddress
    delivery: SourceAddress | None = None

    @field_validator("company")
    @classmethod
    def company_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("company must not be blank")
        return v

    @property
    def delivery_same_as_billing(self) -> bool:
        """True only when there is no separate delivery address, or it matches the
        billing address on every field that matters for the role-assignment branch
        (brief S2.8). A differing recipient name (as in the sample fixture) means
        this is False and a second address with the Delivery role is required."""
        if self.delivery is None:
            return True
        b, d = self.billing, self.delivery
        return (
            _norm(b.name) == _norm(d.name)
            and _norm(b.street) == _norm(d.street)
            and _norm(b.zip) == _norm(d.zip)
            and _norm(b.city) == _norm(d.city)
            and _norm(b.country) == _norm(d.country)
        )


class SourcePayment(BaseModel):
    method: str
    paid_status: Literal["PAID", "UNPAID"]
    payment_date: date | None = None

    @field_validator("method")
    @classmethod
    def method_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("payment method must not be blank")
        return v

    @model_validator(mode="after")
    def paid_requires_date(self) -> SourcePayment:
        if self.paid_status == "PAID" and self.payment_date is None:
            raise ValueError(
                "paid_status is PAID but payment_date is missing -- contradictory "
                "input (brief S5.3 forbids inventing a date but requires one when "
                "PAID); this must be resolved by re-reading the source, not guessed"
            )
        return self


class SourceItem(BaseModel):
    position: int
    sku: str
    description: str
    quantity: Decimal
    unit: str | None = None
    unit_net_price: Decimal
    discount_pct: Decimal = Decimal("0")
    vat_pct: Decimal
    line_net_total: Decimal

    @field_validator("sku")
    @classmethod
    def sku_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sku must not be blank")
        return v

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description must not be blank")
        return v

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError(f"quantity must be > 0, got {v}")
        return v

    @field_validator("unit_net_price")
    @classmethod
    def price_not_negative(cls, v: Decimal) -> Decimal:
        # zero is a legitimate promo/free line; negative is not
        if v < 0:
            raise ValueError(f"unit_net_price must be >= 0, got {v}")
        return v

    @field_validator("discount_pct")
    @classmethod
    def discount_in_range(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v <= Decimal("100")):
            raise ValueError(f"discount_pct out of [0,100]: {v}")
        return v

    @field_validator("vat_pct")
    @classmethod
    def vat_not_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError(f"vat_pct must be >= 0, got {v}")
        return v

    def expected_line_net(self) -> Decimal:
        return q2(
            self.quantity * self.unit_net_price * (Decimal(1) - self.discount_pct / 100)
        )

    def product_master_gross(self) -> Decimal:
        """Brief S3.9: the Product master's gross price ignores this transaction
        line's discount -- it's unit_net_price grossed up by VAT only."""
        return q2(self.unit_net_price * (Decimal(1) + self.vat_pct / 100))

    def vat_pct_str(self) -> str:
        """The VAT percentage as a plain decimal string safe to TYPE INTO a
        field: "20", not "2E+1".

        `str(Decimal("20").normalize())` yields `'2E+1'` -- scientific
        notation, because normalize() strips the trailing zero into an
        exponent. Fakturama parses that as **0**, so writing it into the TAX
        Rate editor's Value field silently created a 0% tax rate named
        "VAT 20%": the name looked right, every later lookup matched it, and
        the error only surfaced as an order line showing "0 %". Confirmed live
        on a 20% French order. This affects every rate that is a multiple of
        ten (10, 20, 30 ...); 19% happened to be safe, which is why it went
        unnoticed for so long.

        Kept next to vat_name() deliberately -- vat_name() already guarded
        against exactly this with format(..., "f") and the fix was simply
        never applied to the value as well.
        """
        return format(self.vat_pct.normalize(), "f")

    def vat_name(self) -> str:
        """Canonical 'VAT {pct}%' string, decimal-separator-normalized, so repeat
        lookups always match what was created (design doc P3)."""
        plain = self.vat_pct_str()
        pct_str = plain.rstrip("0").rstrip(".") if "." in plain else plain
        return f"VAT {pct_str}%"


class SourceOrder(BaseModel):
    external_reference: str
    order_date: date
    currency: str = "EUR"
    order_discount_pct: Decimal = Decimal("0")
    shipping_amount: Decimal = Decimal("0")
    debtor: SourceDebtor
    payment: SourcePayment
    items: list[SourceItem] = Field(min_length=1)
    net_total: Decimal
    vat_total: Decimal
    gross_total: Decimal
    confidence: dict[str, float] = Field(default_factory=dict)

    @field_validator("external_reference")
    @classmethod
    def ext_ref_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("external_reference must not be blank")
        return v


def _norm(s: str) -> str:
    """Normalization used ONLY for equality comparisons (exact-match decisions,
    delivery==billing check). Never used for what actually gets typed into
    Fakturama -- that always uses the verbatim extracted value."""
    return " ".join(s.strip().casefold().split())