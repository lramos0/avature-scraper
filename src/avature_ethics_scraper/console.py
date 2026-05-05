"""Rich console presentation helpers."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlparse

if sys.platform == "win32":
    import msvcrt
else:
    import termios
    import tty

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from .legal import ACK_PHRASE, LEGAL_WARNING
from .models import FetchResult, RobotsDecision, ScrapeReport

console = Console()

T = TypeVar("T")


def banner() -> None:
    title = Text("aventure-scraper", style="bold bright_cyan")
    subtitle = Text("robots.txt-first • polite • cached • progressive fetch", style="bright_white")
    console.print(Panel.fit(Text.assemble(title, "\n", subtitle), border_style="bright_blue", padding=(1, 4)))


def show_robots(decision: RobotsDecision, *, verbose: bool = False) -> None:
    """Display a robots.txt decision.

    Production default is intentionally quiet: allowed checks are one-line confirmations,
    while disallowed checks always get the full legal/context panel.
    """
    style = "green" if decision.allowed else "bold red"
    if decision.allowed and not verbose:
        console.print(
            f"[green]✓[/] robots.txt allows [bold]{_compact_url(decision.url)}[/]",
            highlight=False,
        )
        return

    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Target", decision.url)
    table.add_row("Robots", decision.robots_url)
    table.add_row("User-Agent", decision.user_agent)
    table.add_row("Decision", f"[{style}]{'Allowed' if decision.allowed else 'Disallowed'}[/{style}]")
    table.add_row("Reason", decision.reason)
    console.print(Panel(table, title="Robots Check", border_style=style))


def run_with_inference_status(label: str, timeout_seconds: float, operation: Callable[[], T]) -> T:
    """Run a potentially slow inference/fetch step with a live Rich status indicator."""
    message = (
        f"[bold bright_cyan]Inferring achievable data via {label}[/] "
        f"[dim](timeout: {timeout_seconds:g}s)[/]"
    )
    with console.status(message, spinner="dots12", spinner_style="bright_cyan"):
        return operation()


def polite_delay(seconds: float) -> None:
    if seconds <= 0:
        return
    with console.status(
        f"[bold bright_cyan]Respecting polite crawl delay[/] [dim]({seconds:g}s)[/]",
        spinner="line",
        spinner_style="bright_cyan",
    ):
        time.sleep(seconds)


def require_legal_acknowledgement() -> bool:
    console.print(Panel(LEGAL_WARNING, title="Legal Responsibility Gate", border_style="bold red"))
    typed = Prompt.ask("Acknowledgement phrase", default="")
    return typed.strip() == ACK_PHRASE


def choose_existing_output_action(path: str, cached_jobs: int) -> str:
    """Return 'continue' or 'overwrite' for an existing output file.

    The previous Rich panel looked great in a normal terminal, but some shells
    captured every refresh as a new panel. This version uses a small static
    header plus one raw ANSI line that updates in place.
    """
    if not sys.stdin.isatty():
        console.print(
            f"[yellow]![/] Existing output cache found at [bold]{path}[/]; non-interactive terminal detected. Continuing from cache."
        )
        return "continue"

    selected = 0
    console.print()
    console.print("[bold bright_cyan]Output cache found[/]")
    console.print(f"[bright_white]Path:[/] [bold]{path}[/]")
    console.print(f"[bright_white]Cached jobs:[/] [bold]{cached_jobs}[/]")
    console.print("[dim]Enter = select • s / ↓ / → = overwrite • ↑ / ← = continue[/]")

    def render_line() -> str:
        clear = "\x1b[2K"
        continue_style = "\x1b[1;96m" if selected == 0 else "\x1b[37m"
        overwrite_style = "\x1b[1;93m" if selected == 1 else "\x1b[37m"
        reset = "\x1b[0m"
        return (
            f"\r{clear}"
            f"{continue_style}{'▶' if selected == 0 else ' '} Continue from cache{reset}"
            f"   {overwrite_style}{'▶' if selected == 1 else ' '} Overwrite and start fresh{reset}"
        )

    sys.stdout.write(render_line())
    sys.stdout.flush()
    try:
        while True:
            key = _read_key()
            if key in {"\r", "\n"}:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "continue" if selected == 0 else "overwrite"
            if key in {"\x1b[B", "\x1b[C", "s", "S", "j", "J"}:
                selected = 1
            elif key in {"\x1b[A", "\x1b[D", "w", "W", "k", "K"}:
                selected = 0
            sys.stdout.write(render_line())
            sys.stdout.flush()
    finally:
        sys.stdout.write("\x1b[0m")
        sys.stdout.flush()

def show_cache_loaded(path: str, cached_jobs: int) -> None:
    console.print(f"[bright_cyan]↻[/] Continuing from [bold]{path}[/] with [bold]{cached_jobs}[/] cached jobs.")


def show_cache_overwrite(path: str) -> None:
    console.print(f"[yellow]↺[/] Overwriting [bold]{path}[/] and starting fresh.")


def show_cache_write(path: str, job_count: int, *, verbose: bool = False) -> None:
    if verbose:
        console.print(f"[dim]cache write → {path} ({job_count} jobs)[/]")


def show_fetch_attempt(result: FetchResult, *, verbose: bool = False) -> None:
    if result.ok:
        if verbose:
            status = result.status_code if result.status_code is not None else "n/a"
            detail = f"HTTP {status}; content length {len(result.content):,}"
        else:
            detail = _success_detail(result)
        console.print(f"[green]✓[/] Retrieved via [bold green]{result.method.value}[/] {detail}")
        return

    status = result.status_code if result.status_code is not None else "n/a"
    detail = result.error or f"HTTP {status}; content did not look like job/listing data"
    console.print(f"[yellow]↳[/] [bold yellow]{result.method.value}[/] did not produce usable job data — {detail}")


def explain_next_fetch(next_method: str) -> None:
    console.print(
        Panel(
            f"The previous method did not return recognizable job/listing content.\n\n"
            f"Press Enter to continue with [bold bright_cyan]{next_method}[/].",
            title="Fallback Needed",
            border_style="yellow",
        )
    )
    if sys.stdin.isatty():
        input()
    else:
        console.print(
            "[dim]Non-interactive stdin: continuing automatically to the next fetch method.[/]"
        )


def show_summary(report: ScrapeReport) -> None:
    table = Table(title="Scrape Summary", border_style="bright_blue")
    table.add_column("Metric", style="bold bright_white")
    table.add_column("Value", style="bright_cyan")
    table.add_row("Target", report.target_url)
    table.add_row("Landing fetch", report.landing_page.method.value if report.landing_page else "none")
    table.add_row("Discovered job URLs", str(len(report.discovered_job_urls)))
    table.add_row("Fetched job summaries", str(len(report.jobs)))
    table.add_row("Warnings", str(len(report.warnings)))
    if report.output_fields:
        table.add_row("Job columns", ", ".join(report.output_fields))
    console.print(table)

    if report.jobs:
        jobs_table = Table(title="Extracted Jobs", border_style="green")
        jobs_table.add_column("#", justify="right", style="bold")
        jobs_table.add_column("id", overflow="fold")
        jobs_table.add_column("Title")
        jobs_table.add_column("Location")
        jobs_table.add_column("Description", overflow="fold", max_width=48)
        jobs_table.add_column("Review")
        jobs_table.add_column("URL", overflow="fold")
        for index, job in enumerate(report.jobs, start=1):
            desc = (job.description or job.description_preview or "")[:120]
            if len(job.description or "") > 120 or len(job.description_preview or "") > 120:
                desc = (desc[:117] + "…") if desc else "—"
            review = "yes" if job.needs_manual_review else "—"
            jobs_table.add_row(
                str(index),
                job.id or job.requisition_id or "—",
                job.title or "—",
                job.location or "—",
                desc or "—",
                review,
                job.url,
            )
        console.print(jobs_table)


def show_job_progress(
    current: int,
    total: int,
    url: str,
    *,
    cached: bool = False,
    verbose: bool = False,
) -> None:
    status = "cached" if cached else "scraping"
    line = (
        f"\n[bold bright_white]Job {current}/{total}[/] "
        f"[{ 'bright_cyan' if cached else 'bright_magenta'}]{status}[/]"
    )
    if verbose:
        line += f" [dim]{_compact_url(url, max_path=72)}[/]"
    console.print(line)


def cat_progress_line(current: int, total: int, *, width: int = 28, angry: bool = False) -> Text:
    if total <= 0:
        total = 1
    progress = min(max(current / total, 0.0), 1.0)
    cat_index = min(width - 1, int(progress * (width - 1)))
    cells = ["·"] * width
    cells[cat_index] = "ᓚᘏᗢ"
    line = "".join(cells)
    return Text.assemble(
        ("[", "dim"),
        (line, "bright_red" if angry else "bright_magenta"),
        ("$", "bold bright_green"),
        ("]", "dim"),
        (f" {current}/{total}", "bright_white"),
    )


class CatProgress:
    """Small live progress renderer that updates in place instead of printing rows."""

    def __init__(self, total: int, *, angry: bool = False) -> None:
        self._total = total
        self._angry = angry
        self._live = Live(
            cat_progress_line(0, total, angry=angry),
            console=console,
            refresh_per_second=12,
            transient=False,
        )

    def __enter__(self) -> "CatProgress":
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self._live.__exit__(exc_type, exc, traceback)

    def update(self, current: int) -> None:
        self._live.update(cat_progress_line(current, self._total, angry=self._angry))


def start_cat_progress(total: int, *, angry: bool = False) -> CatProgress:
    return CatProgress(total, angry=angry)


def show_cat_progress(current: int, total: int) -> None:
    # Backward-compatible helper for callers that are not inside a Live region.
    console.print(cat_progress_line(current, total))


def _success_detail(result: FetchResult) -> str:
    status = result.status_code if result.status_code is not None else "n/a"
    kb = len(result.content.encode("utf-8")) / 1024 if result.content else 0
    return f"[dim](HTTP {status}, {kb:.1f} KB)[/]"


def _compact_url(url: str, *, max_path: int = 60) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if len(path) > max_path:
        path = f"{path[: max_path - 1]}…"
    return f"{parsed.netloc}{path}"


def _read_key() -> str:
    if sys.platform == "win32":
        first = msvcrt.getwch()
        if first in {"\x00", "\xe0"}:
            second = msvcrt.getwch()
            return {
                "H": "\x1b[A",
                "P": "\x1b[B",
                "K": "\x1b[D",
                "M": "\x1b[C",
            }.get(second, second)
        return "\n" if first == "\r" else first

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch1 = sys.stdin.read(1)
        if ch1 == "\x1b":
            ch2 = sys.stdin.read(1)
            ch3 = sys.stdin.read(1)
            return ch1 + ch2 + ch3
        return ch1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
