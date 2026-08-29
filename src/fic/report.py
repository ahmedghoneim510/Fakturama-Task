"""Per-run artifacts: a timestamped trace and a rendered report.md, built up
continuously during the run rather than staged at the end -- this is what makes
the "annotated screenshots" deliverable a curation task instead of a scramble.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "runs"


def new_run_dir() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = RUNS_DIR / ts
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    return run_dir


def write_extraction(run_dir: Path, order) -> None:
    (run_dir / "extraction.json").write_text(
        order.model_dump_json(indent=2), encoding="utf-8"
    )


def write_state(run_dir: Path, state) -> None:
    (run_dir / "state.json").write_text(
        json.dumps(asdict(state), indent=2, default=str), encoding="utf-8"
    )


def append_trace(run_dir: Path, event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with (run_dir / "trace.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def render_report(run_dir: Path, state, outcome: str, error: str | None = None) -> Path:
    lines = [
        f"# Run report — {run_dir.name}",
        "",
        f"**Outcome:** {outcome}",
        "",
    ]
    if error:
        lines += ["## Error", "", f"```\n{error}\n```", ""]
    lines += ["## Log", ""]
    for entry in getattr(state, "log", []):
        lines.append(f"- {entry}")
    lines += ["", "## Screenshots", ""]
    shots = sorted((run_dir / "screenshots").glob("*.png"))
    if shots:
        for shot in shots:
            lines.append(f"![{shot.stem}](screenshots/{shot.name})")
    else:
        lines.append("_none captured this run_")
    report_path = run_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
