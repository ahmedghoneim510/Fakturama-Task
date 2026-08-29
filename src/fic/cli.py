"""CLI entry point: `fic run|extract|probe`."""
from __future__ import annotations

import os
import traceback
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

# Which vision model reads the order image. Overridable per-run with
# `--provider`, or globally with FIC_PROVIDER in .env, so switching does not
# mean editing code. Read after load_dotenv() so .env wins.
DEFAULT_PROVIDER = os.environ.get("FIC_PROVIDER", "gemini").strip().lower()

app = typer.Typer(add_completion=False, help="Fakturama Image-to-Cash Automation")
console = Console()


@app.command()
def extract(
    image: Path = typer.Argument(..., exists=True, help="Path to the order image"),
    provider: str = typer.Option(DEFAULT_PROVIDER, help="claude | gemini"),
    out: Path | None = typer.Option(None, "-o", "--out", help="Write JSON here instead of stdout"),
):
    """Extract + reconcile a source order image, no UI involved at all."""
    from fic.extraction import extract_and_reconcile

    order = extract_and_reconcile(image, provider=provider)
    text = order.model_dump_json(indent=2)
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]wrote {out}[/green]")
    else:
        console.print(text)


@app.command()
def probe():
    """Dump the live UIA tree of Fakturama's focused/main window -- essential
    for calibrating config/selectors.yaml against the real app rather than
    guessing. Requires Fakturama to already be running with an active window."""
    from fic.uia.locator import snapshot
    from fic.uia.session import FakturamaSession

    session = FakturamaSession()
    session.launch_or_attach()
    console.print(f"[bold]{session.main_window.window_text()}[/bold]")
    for node in snapshot(session.main_window):
        console.print(
            f"  [{node.control_type:12}] {node.name!r:40} rect={node.rect} enabled={node.enabled}"
        )


@app.command()
def run(
    image: Path | None = typer.Argument(None, exists=True, help="Path to the order image"),
    from_json: Path | None = typer.Option(
        None,
        "--from-json",
        exists=True,
        help="Skip extraction and use an already-extracted order JSON (no API key needed)",
    ),
    provider: str = typer.Option(DEFAULT_PROVIDER, help="claude | gemini"),
    cross_check: bool = typer.Option(False, help="Extract with both providers and diff money fields"),
    dry_run: bool = typer.Option(
        False, help="Extract + reconcile + resolve every locator, click nothing that mutates"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Run even if an order with this Cust.Ref was already saved (creates a duplicate)",
    ),
):
    """Full flow: order image -> saved, verified Order + Invoice in Fakturama.

    `--from-json` replaces only the extraction step, feeding a SourceOrder JSON
    (the same shape `fic extract` writes) straight into the flow. Everything
    downstream is identical, tier-2 reconciliation included -- the JSON is
    validated exactly as a freshly-extracted order would be, so this is a way
    to skip the LLM, not a way to skip the checks. Useful for re-running a
    known payload deterministically, and for working without an API key.
    """
    from fic.errors import AlreadyProcessedError, AutomationError
    from fic.extraction import extract_and_reconcile, reconcile, self_consistency_check
    from fic.flow import run_flow
    from fic.models import SourceOrder
    from fic.report import append_trace, new_run_dir, render_report, write_extraction, write_state
    from fic.uia.session import FakturamaSession

    if from_json is None and image is None:
        console.print("[red]give either an IMAGE argument or --from-json PATH[/red]")
        raise typer.Exit(2)
    if from_json is not None and image is not None:
        console.print("[red]give an IMAGE or --from-json, not both[/red]")
        raise typer.Exit(2)

    run_dir = new_run_dir()
    console.print(f"[dim]run directory: {run_dir}[/dim]")
    from fic.flow import RunState

    state = RunState()  # populated once run_flow starts; kept in scope for the
    # except-block below so a mid-flow failure still renders whatever log
    # entries were recorded before it happened, not an empty report.

    try:
        if from_json is not None:
            order = SourceOrder.model_validate_json(from_json.read_text(encoding="utf-8"))
            reconcile(order)  # same tier-2 gate as an extracted order -- never skipped
            console.print(f"[green]loaded + reconciled {from_json}[/green]")
        else:
            order = self_consistency_check(image) if cross_check else extract_and_reconcile(
                image, provider=provider
            )
            console.print("[green]extraction + reconciliation OK[/green]")
        write_extraction(run_dir, order)

        if dry_run:
            console.print("[yellow]--dry-run: stopping before any UI interaction[/yellow]")
            raise typer.Exit(0)

        # Idempotency BEFORE the session, not inside run_flow. The check itself
        # was already correct, but it ran after launch_or_attach() -- so a
        # refusal still cost a full Fakturama cold start (1-2 minutes) before
        # printing an answer that needed no UI at all. Reading the saved
        # documents needs nothing but a file, so it belongs before anything is
        # opened. run_flow keeps its own copy of the check for callers that use
        # it directly; the call is pure, so running it twice costs nothing.
        if not from_json or True:  # applies to both input paths
            from fic.uia.contact_resolver import find_orders_by_reference

            if not force:
                already = find_orders_by_reference(order.external_reference)
                if already:
                    raise AlreadyProcessedError(
                        f"an order with Cust.Ref {order.external_reference!r} has "
                        f"already been saved ("
                        f"{', '.join(d['document'] for d in already)}) -- re-running "
                        f"would create a duplicate for the same purchase. Pass "
                        f"--force to run anyway.",
                        external_reference=order.external_reference,
                        existing_documents=already,
                    )

        session = FakturamaSession()
        session.launch_or_attach()
        state = run_flow(session, order, force=force)
        write_state(run_dir, state)
        render_report(run_dir, state, outcome="done, verified")
        console.print("[bold green]done — Order + Invoice saved and verified[/bold green]")
        raise typer.Exit(0)

    except typer.Exit:
        # typer signals a normal exit by raising -- it must not be caught by the
        # generic handler below, which would report a clean run (a successful
        # --dry-run, or a completed flow) as "unexpected error" and print a
        # traceback for it. Confirmed by running --dry-run to completion.
        raise

    except AutomationError as exc:
        append_trace(run_dir, {"error": type(exc).__name__, "message": exc.message, "evidence": exc.evidence})
        render_report(
            run_dir, state, outcome=f"{type(exc).__name__}: {exc.message}", error=str(exc.evidence)
        )
        console.print(f"[bold red]{type(exc).__name__}[/bold red]: {exc.message}")
        console.print(exc.evidence)
        raise typer.Exit(exc.exit_code)

    except Exception:
        render_report(run_dir, state, outcome="unexpected error", error=traceback.format_exc())
        console.print("[bold red]unexpected error[/bold red]")
        console.print(traceback.format_exc())
        raise typer.Exit(1)


if __name__ == "__main__":
    app()