"""Extraction pipeline tests. The mocked test runs in CI with zero network; the
`live` test actually calls Claude and needs ANTHROPIC_API_KEY in .env -- run it
explicitly with `pytest -m live` once you've added your key."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fic.extraction import parse_extraction, reconcile
from fic.models import SourceOrder

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "golden" / "order_001.json"
SAMPLE_IMAGE = Path(__file__).resolve().parents[1] / "data" / "input" / "order_001.png"


def test_parse_extraction_accepts_valid_tool_output():
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    order = parse_extraction(data, source="test", model="n/a")
    assert isinstance(order, SourceOrder)
    reconcile(order)


def test_parse_extraction_rejects_malformed_output():
    from fic.errors import ExtractionError

    bad = {"external_reference": ""}  # missing everything required
    with pytest.raises(ExtractionError):
        parse_extraction(bad, source="test", model="n/a")


def test_extract_with_claude_uses_forced_tool_and_validates(monkeypatch):
    """Exercises extract_with_claude()'s parsing path with the Anthropic client
    mocked out -- proves the tool-forcing + validation wiring works without
    hitting the network or needing an API key."""
    from fic.extraction import extract_with_claude

    golden_data = json.loads(GOLDEN.read_text(encoding="utf-8"))

    fake_tool_use = MagicMock()
    fake_tool_use.type = "tool_use"
    fake_tool_use.input = golden_data
    fake_response = MagicMock()
    fake_response.content = [fake_tool_use]

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    monkeypatch.setattr(
        "anthropic.Anthropic", lambda *a, **k: fake_client, raising=False
    )

    order = extract_with_claude(SAMPLE_IMAGE, model="claude-opus-5")
    assert order.external_reference == "WEB-2026-0714-A17"
    # the tool call really was forced to record_order
    _, kwargs = fake_client.messages.create.call_args
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_order"}


@pytest.mark.live
def test_live_claude_extraction_matches_golden_fixture():
    """Requires ANTHROPIC_API_KEY. Run explicitly: `pytest -m live`."""
    from fic.extraction import extract_and_reconcile

    order = extract_and_reconcile(SAMPLE_IMAGE, provider="claude")
    golden = SourceOrder.model_validate(json.loads(GOLDEN.read_text(encoding="utf-8")))

    assert order.external_reference == golden.external_reference
    assert order.debtor.company == golden.debtor.company
    assert abs(order.gross_total - golden.gross_total) <= Decimal("0.02")
    assert len(order.items) == len(golden.items)
