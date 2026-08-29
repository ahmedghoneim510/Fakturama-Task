"""Launch/attach to Fakturama, find the main window, and provide the current
editor-tab lookup used to enforce 'the New Order tab stays open across every
detour' -- the current Order tab is identified by its order number, never by
'whatever tab is currently active'.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fic.errors import ControlNotFound
from fic.uia.waits import wait_until

_DEFAULT_CANDIDATES = [
    r"C:\Program Files (x86)\Fakturama\Fakturama.exe",
    r"C:\Program Files\Fakturama\Fakturama.exe",
]


def find_fakturama_exe() -> str:
    env_path = os.environ.get("FIC_FAKTURAMA_EXE")
    if env_path and Path(env_path).exists():
        return env_path
    for candidate in _DEFAULT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise ControlNotFound(
        "could not find Fakturama.exe -- set FIC_FAKTURAMA_EXE in .env",
        checked=[env_path] + _DEFAULT_CANDIDATES,
    )


class FakturamaSession:
    """Owns the connection to the running Fakturama process and its main window."""

    def __init__(
        self,
        exe_path: str | None = None,
        *,
        timeout: float = 30.0,
        startup_timeout: float = 180.0,
    ):
        self.exe_path = exe_path or find_fakturama_exe()
        self.timeout = timeout
        # Separate, much larger budget for "the workspace has finished
        # loading" -- see launch_or_attach(). A cold Eclipse RCP start is
        # minutes-scale slow on a modest machine; a warm attach satisfies
        # this immediately, so a generous value costs nothing in the common
        # case and removes a whole class of cold-start flake.
        self.startup_timeout = startup_timeout
        self.app = None
        self.main_window = None

    def launch_or_attach(self):
        from pywinauto.application import Application

        app = Application(backend="uia")
        try:
            app.connect(path=self.exe_path, timeout=2)
            self.app = app
            print("  · attached to the running Fakturama", flush=True)
        except Exception:
            # Say so, and say how long it will take. A cold start blocks here
            # for a minute or more while the Eclipse workspace and embedded
            # database load, and printing nothing during that made a working
            # run look frozen -- an operator killed one for exactly this reason.
            print(
                "  · Fakturama is not running -- launching it.\n"
                "    A cold start takes 1-2 minutes (Eclipse workspace + database).\n"
                "    Tip: start Fakturama yourself and leave it open; attaching is instant.",
                flush=True,
            )
            self.app = Application(backend="uia").start(self.exe_path)

        # Find the window and confirm it's usable in ONE polled step, re-finding
        # the window on every attempt.
        #
        # An earlier version bound `main_window` once and then polled that fixed
        # reference for a toolbar. On a cold start that is a trap: Fakturama
        # shows a splash/early window that ALSO matches the title test, so the
        # session latched onto it and waited the full 180s for a toolbar that
        # would never appear inside it -- while the real main window opened
        # unnoticed beside it. The failure was silent by construction
        # (`last_exception: None`), because the toolbar lookup was returning
        # "not yet", not raising.
        #
        # Re-finding each time fixes it and makes "is this the right window?"
        # and "has it finished loading?" the same question: the window that
        # HAS the toolbar is by definition the real main window. A splash can
        # never satisfy it.
        #
        # The budget is separate from, and much larger than, self.timeout:
        # a cold start (Eclipse RCP workspace + embedded HSQLDB) takes minutes,
        # while a warm attach satisfies this immediately.
        from fic.uia.locator import find_by_name

        deadline_note = {"last": 0.0}
        started = time.monotonic()

        def _ready_window():
            # EVERY candidate is tried, not just the first title match: during
            # startup a splash and the real window can both be present and both
            # match the title test, and picking the first would keep returning
            # the splash forever.
            for win in self._candidate_windows():
                try:
                    find_by_name(win, "Create: New Order", control_type="Button")
                except Exception:
                    continue
                return win
            # Heartbeat, so a slow start looks like progress rather than a
            # hang -- the whole reason this step prints at all.
            elapsed = time.monotonic() - started
            if elapsed - deadline_note["last"] >= 15:
                deadline_note["last"] = elapsed
                print(f"    ... still loading ({int(elapsed)}s)", flush=True)
            return None

        print("  · waiting for Fakturama's workspace to finish loading ...", flush=True)
        self.main_window = wait_until(
            _ready_window,
            timeout=self.startup_timeout,
            poll=1.0,
            description="Fakturama main window with its toolbar loaded",
        )
        try:
            self.main_window.set_focus()
        except Exception:
            pass
        print("  · Fakturama ready", flush=True)
        return self.main_window

    def _candidate_windows(self):
        """Every top-level window that could be Fakturama's main window.

        Returns a LIST, because "looks like Fakturama" is not sufficient to
        identify the real one: a leftover installer window can be present
        (excluded here by name), and during startup a splash/early window can
        match too (not distinguishable by title at all -- the caller separates
        it by checking which window actually contains the toolbar).
        """
        from pywinauto import Desktop

        found = []
        try:
            windows = Desktop(backend="uia").windows()
        except Exception:
            return found
        for w in windows:
            try:
                text = w.window_text() or ""
            except Exception:
                continue
            lowered = text.lower()
            if lowered.startswith("fakturama") and "install" not in lowered:
                found.append(w)
        return found

    def _find_main_window(self):
        """First candidate, for callers that just need any Fakturama window.
        Prefer _candidate_windows() plus a content check when it matters."""
        candidates = self._candidate_windows()
        return candidates[0] if candidates else None

    def editor_tab(self, title_contains: str):
        """Click the TabItem whose title contains `title_contains` (e.g. an
        order number) to make it active, then return `main_window` as the
        field-resolution scope.

        KNOWN LIMITATION (see README "what I skipped"): the TabItem header
        element does not contain the tab's content fields as descendants in
        this UIA tree -- confirmed live, resolve_by_label against the TabItem
        itself finds nothing. The content pane for a specific tab could not be
        isolated in the time available, so this scopes to the whole main
        window instead. That's safe as long as only one editor tab is open at
        a time (the only case this build's flow ever creates), since Fakturama
        only keeps one tab's fields enabled/visible at once -- it stops being
        safe if multiple Order/Debtor/Product tabs are ever open simultaneously,
        since label-anchored resolution would then see duplicate labels across
        tabs and correctly refuse as ambiguous rather than silently guessing.
        """

        def _find_tab():
            for tab in self.main_window.descendants(control_type="TabItem"):
                try:
                    text = tab.window_text() or ""
                except Exception:
                    continue
                if title_contains.lower() in text.lower():
                    return tab
            return None

        tab = wait_until(
            _find_tab, timeout=10.0, description=f"editor tab containing {title_contains!r}"
        )
        try:
            tab.click_input()
        except Exception:
            pass
        return self.main_window

    def wait_for_any_tab(self, *title_options: str, timeout: float = 15.0):
        """Try each candidate tab title in turn (e.g. a document type whose
        exact wording varies by Fakturama version -- 'New Order' vs 'Order'),
        polling all of them together within one overall timeout budget rather
        than exhausting a full per-candidate timeout on each in sequence."""

        def _find():
            for title in title_options:
                for tab in self.main_window.descendants(control_type="TabItem"):
                    try:
                        text = tab.window_text() or ""
                    except Exception:
                        continue
                    if title.lower() in text.lower():
                        try:
                            tab.click_input()
                        except Exception:
                            pass
                        return self.main_window
            return None

        return wait_until(
            _find, timeout=timeout, description=f"any editor tab in {title_options!r}"
        )

    def close(self):
        # Deliberately does not kill the Fakturama process -- the operator's live
        # session and any unsaved manual work outside this flow must not be torn
        # down by the automation exiting.
        self.app = None
        self.main_window = None
