"""Exception hierarchy for the automation.

Every exception carries structured `evidence` for the run report — an operator
reading `runs/<id>/report.md` needs to know *why* a run stopped, not just *that* it
stopped. `exit_code` is what `cli.py` returns to the shell.

Rule: ManualReviewRequired (and its subclasses) is a first-class terminal outcome,
never caught-and-retried. "An ambiguous debtor is ambiguous the second time too."
Only transient UI slowness (inside wait_until) is eligible for a bounded retry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class AutomationError(Exception):
    """Base for everything the flow can raise."""

    exit_code: int = 1

    def __init__(self, message: str, **evidence: Any) -> None:
        super().__init__(message)
        self.message = message
        self.evidence = evidence

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}({self.message!r}, evidence={self.evidence!r})"


class ExtractionError(AutomationError):
    """Extraction or validation (tier 1 pydantic / tier 2 reconcile) failed before
    any UI interaction began. Nothing in Fakturama has been touched."""

    exit_code = 5


class ManualReviewRequired(AutomationError):
    """A business rule says stop — ambiguous match, unrecognized payment status,
    contradictory data, cross-provider disagreement."""

    exit_code = 2

    def __init__(
        self,
        reason: str,
        *,
        candidates: list[Any] | None = None,
        screenshot: Path | None = None,
        **evidence: Any,
    ) -> None:
        super().__init__(
            reason,
            candidates=candidates or [],
            screenshot=str(screenshot) if screenshot else None,
            **evidence,
        )


class AmbiguousControl(ManualReviewRequired):
    """The grounding engine found >=2 candidates within the ambiguity margin of each
    other. Refusing to guess between them is the point, not a bug."""


class OptionUnavailable(ManualReviewRequired):
    """An exact dropdown/combo value was required and genuinely absent (e.g. the
    Invoice's Payment combo does not offer the method the source document names)."""


class VerificationFailed(AutomationError):
    """A write did not read back as intended, or Save did not clear the dirty flag."""

    exit_code = 3


class ControlNotFound(AutomationError):
    """The grounding engine exhausted its strategy cascade without resolving a
    required control. Distinct from AmbiguousControl: zero candidates, not many."""

    exit_code = 4


class AlreadyProcessedError(AutomationError):
    """Pre-flight found an existing Order with this External Reference already in
    Data > Documents. The run is a safe no-op unless invoked with --force."""

    exit_code = 6


EXIT_CODE_MEANING: dict[int, str] = {
    0: "done, verified",
    1: "unexpected error",
    2: "manual review required",
    3: "verification failed",
    4: "control not found",
    5: "extraction/validation error",
    6: "already processed (idempotency guard)",
}
