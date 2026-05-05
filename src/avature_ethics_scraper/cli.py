"""Command-line interface for avature-scraper."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from .cache import ReportCache, ReportCacheError
from .console import (
    banner,
    choose_existing_output_action,
    console,
    show_cache_loaded,
    show_cache_overwrite,
    show_summary,
)
from .csv_jobpool import export_csv_and_maybe_upload
from .fetchers import apply_wslg_display_if_needed
from .git_identity import resolve_cache_user_key
from .output_spec import OutputSpec
from .scraper import EthicalAvatureScraper, ScrapeSettings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Ethical Avature career-site scraper with robots.txt checks, output caching, and progressive fetch fallbacks.",
)

DEFAULT_USER_AGENT = "avature-scraper/0.2.4 (+https://example.com/contact)"


def _cache_action_override() -> str | None:
    raw = (os.environ.get("AVATURE_CACHE_ACTION") or "").strip().lower()
    if raw in {"continue", "overwrite"}:
        return raw
    return None


@app.command()
def main(
    target_url: Annotated[str, typer.Argument(help="Career-site or job-detail URL to inspect.")],
    job_url: Annotated[
        list[str] | None,
        typer.Option("--job-url", help="Specific job detail URL. Can be passed multiple times."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="JSON report output path. Existing files are used as the seen-job cache."),
    ] = Path("avature_jobs.json"),
    user_agent: Annotated[
        str,
        typer.Option("--user-agent", help="User-Agent used for robots.txt and fetch requests."),
    ] = DEFAULT_USER_AGENT,
    delay: Annotated[
        float,
        typer.Option(
            "--delay",
            "--request-delay",
            help="Polite request delay in seconds between job detail requests.",
        ),
    ] = 2.0,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Extended request/browser timeout in seconds after the 5-second fast probe."),
    ] = 10.0,
    max_jobs: Annotated[
        int,
        typer.Option("--max-jobs", help="Maximum job detail URLs to fetch."),
    ] = 25,
    allow_external_hosts: Annotated[
        bool,
        typer.Option("--allow-external-hosts", help="Allow discovered job URLs on hosts other than the target host."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Show full robots.txt panels, cache writes, and detailed fetch diagnostics."),
    ] = False,
    browser_path: Annotated[
        str | None,
        typer.Option(
            "--browser-path",
            help="Path to Chrome/Chromium executable for playwrong (required when auto-detection fails).",
        ),
    ] = None,
    browser_engine: Annotated[
        str,
        typer.Option(
            "--browser-engine",
            help="Browser engine for playwrong: chromium or firefox.",
            case_sensitive=False,
        ),
    ] = "chromium",
    prefer_open_browser: Annotated[
        bool,
        typer.Option(
            "--prefer-open-browser",
            help=(
                "Prefer attaching to an already-open debug browser session (Chromium only) "
                "before launching a new one."
            ),
        ),
    ] = False,
    cdp_endpoint: Annotated[
        str | None,
        typer.Option(
            "--cdp-endpoint",
            help=(
                "Attach to this CDP HTTP endpoint (YP-style), e.g. http://127.0.0.1:9222. "
                "Start Edge with --remote-debugging-port=9222 first."
            ),
        ),
    ] = None,
    angry: Annotated[
        bool,
        typer.Option("--angry", help="Easter egg: red cat; also bypasses robots disallow without prompting (legacy)."),
    ] = False,
    allow_disallowed_robots: Annotated[
        bool,
        typer.Option(
            "--allow-disallowed-robots",
            help=(
                "If robots.txt disallows the URL, fetch anyway (no interactive prompt). "
                "Adds a warning to the report. Prefer this over --angry for batch/CI; normal HTTP/headless fallbacks still apply."
            ),
        ),
    ] = False,
    headful_for_each_job: Annotated[
        bool,
        typer.Option(
            "--headful-for-each-job",
            help=(
                "Always start job-detail fetches with headful Playwright. Default skips headful after the first job "
                "succeeds via HTTP (faster bulk runs; then you usually will not see a browser window)."
            ),
        ),
    ] = False,
    discover_only: Annotated[
        bool,
        typer.Option(
            "--discover-only",
            help="Only discover and save job URLs from landing pages; skip all job-detail fetches.",
        ),
    ] = False,
    skills: Annotated[
        bool,
        typer.Option(
            "--skills",
            help="Require a skills column (ld+json, labeled text, or keyword inference); escalates fetch if missing.",
        ),
    ] = False,
    ingestion_date: Annotated[
        bool,
        typer.Option(
            "--ingestion-date",
            help="Emit ingest_utc_date/hour and require job_posted_date from the page when possible (escalates; may substitute after headful — see warnings).",
        ),
    ] = False,
    education_requirements: Annotated[
        bool,
        typer.Option(
            "--education-requirements",
            help="Require education_requirements text (ld+json or labeled/heuristic extraction).",
        ),
    ] = False,
    company_name: Annotated[
        bool,
        typer.Option(
            "--company-name",
            help="Require company_name (ld+json hiringOrganization, og:site_name, or title suffix).",
        ),
    ] = False,
    all_jobdatapool: Annotated[
        bool,
        typer.Option(
            "--all",
            help=(
                "Shorthand for all optional JobDataPool-style columns (skills, ingestion, education, company) plus full export field list. "
                "Best-effort: rich pages with ld+json or clear labels fill cleanly; sparse pages rely on inference, headful fetch, "
                "and may set needs_manual_review or skip rows when a field cannot be satisfied."
            ),
        ),
    ] = False,
    csv_out: Annotated[
        Path | None,
        typer.Option(
            "--csv-out",
            help="Write a JobPool-shaped UTF-8 CSV to this path after a successful run (requires at least one job row).",
        ),
    ] = None,
    upload_to_jobpool: Annotated[
        bool,
        typer.Option(
            "--upload-to-jobpool/--no-upload-to-jobpool",
            help="POST listings JSON to https://jobpool.live/api/scrape-cache (on by default). Use --no-upload-to-jobpool to skip the POST only.",
        ),
    ] = True,
    jobpool_user: Annotated[
        str | None,
        typer.Option(
            "--jobpool-user",
            help="Override user_name sent to JobPool (default: git GitHub username / git config / OS user).",
        ),
    ] = None,
) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
        except (OSError, ValueError):
            pass
    apply_wslg_display_if_needed()
    # Bypass TextIO buffering (notably Windows + subprocess log files).
    try:
        os.write(
            1,
            f"avature-scraper: start {target_url}\n".encode("utf-8", errors="replace"),
        )
    except OSError:
        print(f"avature-scraper: start {target_url}", flush=True)
    banner()
    sys.stdout.flush()
    sys.stderr.flush()

    cache = ReportCache(output)
    cached_report = None
    cache_action_override = _cache_action_override()
    if cache.exists:
        try:
            loaded = cache.load()
            if angry:
                cache_report = loaded
                show_cache_loaded(str(output), len(loaded.jobs))
            else:
                action = cache_action_override or choose_existing_output_action(str(output), len(loaded.jobs))
                if action == "continue":
                    cached_report = loaded
                    show_cache_loaded(str(output), len(loaded.jobs))
                else:
                    show_cache_overwrite(str(output))
        except ReportCacheError as exc:
            console.print(f"[yellow]![/] {exc}")
            action = cache_action_override or choose_existing_output_action(str(output), 0)
            if action == "continue":
                raise typer.Exit(code=2)
            show_cache_overwrite(str(output))

    if all_jobdatapool:
        output_spec = OutputSpec.all_jobdatapool()
    else:
        output_spec = OutputSpec(
            want_skills=skills,
            want_ingestion_date=ingestion_date,
            want_education_requirements=education_requirements,
            want_company_name=company_name,
        )
    settings = ScrapeSettings(
        user_agent=user_agent,
        delay_seconds=delay,
        timeout_seconds=timeout,
        initial_read_timeout_seconds=5.0,
        max_jobs=max_jobs,
        same_host_only=not allow_external_hosts,
        verbose=verbose,
        angry=angry,
        browser_path=browser_path,
        browser_engine=browser_engine.strip().lower(),
        prefer_open_browser=prefer_open_browser,
        cdp_endpoint=(cdp_endpoint or "").strip() or None,
        allow_disallowed_robots=allow_disallowed_robots,
        headful_for_each_job=headful_for_each_job,
        discover_only=discover_only,
        output_spec=output_spec,
    )
    scraper = EthicalAvatureScraper(settings)
    report = scraper.run(target_url, job_urls=job_url or [], cache=cache, cached_report=cached_report)
    scraper.write_report(report, output)
    show_summary(report)

    csv_path = csv_out
    do_upload = upload_to_jobpool
    if csv_path is None and report.jobs:
        csv_path = output.with_suffix(".csv")
    if csv_path is not None:
        user = (jobpool_user or "").strip() or resolve_cache_user_key()
        path, http_ok, msg = export_csv_and_maybe_upload(
            report,
            csv_path,
            upload=do_upload,
            user_name=user,
        )
        if path:
            if http_ok is True:
                style = "bright_green"
            elif http_ok is False:
                style = "yellow"
            else:
                style = "bright_cyan"
            console.print(f"[bold {style}]CSV:[/] {path.resolve()}")


if __name__ == "__main__":
    app()
