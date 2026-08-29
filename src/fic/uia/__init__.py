"""UI Automation grounding layer: session management, control-discovery cascade,
verified interaction primitives, and the item-grid strategy.

DPI awareness is set at import time, unconditionally, before anything else in this
package touches a window handle or a screen coordinate -- otherwise UIA bounding
rectangles and any screenshot pixel coordinates disagree on a scaled display.
"""
from __future__ import annotations

import sys

if sys.platform == "win32":  # pragma: no cover - platform-specific
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
