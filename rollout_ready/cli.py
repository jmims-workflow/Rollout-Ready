"""Command line, for teams that keep prompts in a repo.

  rollout list
  rollout run <suite> --current v2 --new v3 [--live] [--report summary.md]
  rollout serve
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from . import scoring
from .compare import build_comparison, summary_text
from .config import load_settings
from .provider import ClaudeProvider, ProviderError
from .runner import estimate_sentence, pending_for, run_version
from .storage import Store, SuiteError

app = typer.Typer(add_completion=False, help="Rollout Ready: test a prompt change before your team sees it.")


@app.command("list")
def list_suites(suites_dir: Optional[Path] = typer.Option(None, help="Folder that holds the suites.")) -> None:
    """Show suites and their versions."""
    settings = load_settings(suites_dir)
    store = Store(settings.suites_dir)
    for sid in store.suite_ids():
        try:
            suite = store.load_suite(sid)
            cases = store.load_cases(sid)
            versions = store.versions(sid)
        except SuiteError as exc:
            typer.echo(f"{sid}: {exc}", err=True)
            continue
        typer.echo(f"{sid}  {suite.name}  ({len(cases)} test cases)")
        for v in versions:
            typer.echo(f"  {v.id}  {v.summary}")


@app.command()
def run(
    suite: str = typer.Argument(..., help="Suite id, for example followup-email."),
    current: str = typer.Option(..., "--current", help="The version in use now, for example v2."),
    new: str = typer.Option(..., "--new", help="The version to test, for example v3."),
    live: bool = typer.Option(False, "--live", help="Call Claude for any missing results (needs ANTHROPIC_API_KEY)."),
    report: Optional[Path] = typer.Option(None, "--report", help="Also write the summary as Markdown to this file."),
    suites_dir: Optional[Path] = typer.Option(None, help="Folder that holds the suites."),
) -> None:
    """Compare two versions and print the verdict. Exits 1 if the new version should not roll out."""
    settings = load_settings(suites_dir, force_live=live)
    if settings.fallback_notice:
        typer.echo(settings.fallback_notice, err=True)
    store = Store(settings.suites_dir)
    try:
        if settings.is_live:
            todo = pending_for(store, suite, [current, new])
            if todo:
                typer.echo(estimate_sentence(todo, store.load_suite(suite).repeats))
                provider = ClaudeProvider(settings.api_key or "")
                for v in (current, new):
                    run_version(store, suite, v, provider, settings.judge_model,
                                lambda d, t, m: typer.echo(f"  {d}/{t} {m}"))
        comp = build_comparison(store, suite, current, new, settings.is_live)
    except (SuiteError, ProviderError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2)
    if not comp.complete:
        missing = ", ".join(f"{v} ({len(c)} cases)" for v, c in comp.missing.items() if c)
        typer.echo(f"Some results are missing: {missing}. Run again with --live to fill them in.", err=True)
        raise typer.Exit(2)

    typer.echo(f"{comp.suite.name}: {current} vs {new}")
    typer.echo(f"Verdict: {comp.verdict}")
    typer.echo(comp.explanation)
    typer.echo(f"Worse: {comp.worse}  Better: {comp.better}  No clear change: {comp.same}")
    typer.echo(f"Average score: {current} {scoring.fmt(comp.avg_current)}, {new} {scoring.fmt(comp.avg_new)}")
    for r in comp.rows:
        typer.echo(f"  {r.result_label:<16} {scoring.fmt(r.current_score)} -> {scoring.fmt(r.new_score)}  {r.case.name}")
    if report:
        report.write_text(summary_text(comp, markdown=True), encoding="utf-8")
        typer.echo(f"Summary written to {report}")
    if scoring.blocks_rollout(comp.verdict):
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Address to listen on."),
    port: int = typer.Option(int(os.environ.get("PORT", "8080")), help="Port (defaults to PORT or 8080)."),
) -> None:
    """Start the web app."""
    import uvicorn

    uvicorn.run("rollout_ready.web.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
