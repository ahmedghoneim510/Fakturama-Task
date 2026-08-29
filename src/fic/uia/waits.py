"""Condition-based waiting. No sleep() calls anywhere in this codebase -- every wait
is expressed as a predicate that gets polled, per the design docs' non-negotiable
rule: "wait for the list to stabilize" means row count/contents unchanged across
several consecutive polls, not a fixed delay.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TypeVar

from fic.errors import ControlNotFound

T = TypeVar("T")

_UNSET = object()


def wait_until(
    predicate: Callable[[], T | None],
    *,
    timeout: float = 10.0,
    poll: float = 0.15,
    description: str = "condition",
) -> T:
    """Poll `predicate` until it returns a truthy value, or raise ControlNotFound."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # control not yet in the tree, stale element, etc.
            last_exc = exc
            result = None
        if result:
            return result
        time.sleep(poll)
    raise ControlNotFound(
        f"timed out after {timeout}s waiting for {description}",
        last_exception=repr(last_exc) if last_exc else None,
    )


def wait_stable(
    fn: Callable[[], T],
    *,
    stable_for: float = 0.6,
    timeout: float = 10.0,
    poll: float = 0.15,
    description: str = "value",
) -> T:
    """Poll `fn` until its return value is unchanged for `stable_for` seconds
    (used for 'wait for the list to stabilize' in the address/product selectors)."""
    deadline = time.monotonic() + timeout
    last = _UNSET
    stable_since: float | None = None
    while time.monotonic() < deadline:
        current = fn()
        if current == last:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_for:
                return current
        else:
            stable_since = None
        last = current
        time.sleep(poll)
    raise ControlNotFound(f"{description} never stabilized within {timeout}s")


def wait_nested_window(root, title_pattern: str, *, timeout: float = 10.0):
    """Wait for a control_type='Window' DESCENDANT of `root` whose accessible
    name matches `title_pattern`. Live-confirmed: Fakturama's dialogs (Select
    the address, Select a product, ...) are nested Window-type elements inside
    the main application window's own UIA tree -- this is a single-window
    Eclipse RCP app, not one that spawns separate top-level OS windows per
    dialog. Searching Desktop().windows() (the natural first guess) never finds
    them; their OS-level top-level window title is just the generic app name.
    """
    pattern = re.compile(title_pattern, re.IGNORECASE)

    def _find():
        try:
            candidates = root.descendants(control_type="Window")
        except Exception:
            candidates = []
        for w in candidates:
            try:
                text = w.window_text() or ""
            except Exception:
                continue
            if pattern.search(text):
                return w
        return None

    return wait_until(_find, timeout=timeout, description=f"nested window matching {title_pattern!r}")


def wait_nested_window_gone(window, *, timeout: float = 10.0) -> None:
    """Works for both true top-level windows and the nested Window-type
    elements wait_nested_window() returns -- .exists() is valid on either."""
    def _gone():
        try:
            return True if not window.exists() else None
        except Exception:
            return True

    wait_until(_gone, timeout=timeout, description="modal to close")
