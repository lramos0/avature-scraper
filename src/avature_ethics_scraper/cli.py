"""Command-line interface for aventure-scraper."""

from __future__ import annotations

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
from .git_identity import resolve_cache_user_key
from .output_spec import OutputSpec
from .scraper import EthicalAvatureScraper, ScrapeSettings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Ethical Avature career-site scraper with robots.txt checks, output caching, and progressive fetch fallbacks.",
)

DEFAULT_USER_AGENT = "aventure-scraper/0.2.4 (+https://example.com/contact)"


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
    ] = Path("aventure_jobs.json"),
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
    angry: Annotated[
        bool,
        typer.Option("--angry", help="Easter egg: render the progress cat in red."),
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
            help="Shorthand for all optional JobDataPool-style columns (skills, ingestion, education, company) plus full export field list.",
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
    banner()

    cache = ReportCache(output)
    cached_report = None
    if cache.exists:
        try:
            loaded = cache.load()
            if angry:
                cache_report = loaded
                show_cache_loaded(str(output), len(loaded.jobs))
            else:
                action = choose_existing_output_action(str(output), len(loaded.jobs))
                if action == "continue":
                    cached_report = loaded
                    show_cache_loaded(str(output), len(loaded.jobs))
                else:
                    show_cache_overwrite(str(output))
        except ReportCacheError as exc:
            console.print(f"[yellow]![/] {exc}")
            action = choose_existing_output_action(str(output), 0)
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
