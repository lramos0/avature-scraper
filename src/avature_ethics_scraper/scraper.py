"""High-level scraping workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .console import (
    console,
    explain_next_fetch,
    require_legal_acknowledgement,
    run_with_inference_status,
    show_fetch_attempt,
    show_job_progress,
    show_robots,
    polite_delay,
    show_cache_write,
    start_cat_progress,
)
from .extract import (
    apply_headful_inference,
    discover_job_urls,
    extract_listing_discovery_signals,
    extract_job_record,
    is_linkedin_hosted_careers_landing,
    validate_job_page,
)
from .fetchers import FetchSettings, PlaywrightFetcher, RequestsFetcher
from .models import FetchResult, JobSummary, ScrapeReport
from .output_spec import (
    OutputSpec,
    apply_runtime_ingestion_fields,
    missing_default_fields,
    missing_optional_page_fields,
    missing_required_fields,
)
from .cache import ReportCache
from .robots import RobotsPolicy
from .urls import career_landing_url_candidates, normalize_url


@dataclass(frozen=True)
class ScrapeSettings:
    user_agent: str
    delay_seconds: float = 2.0
    timeout_seconds: float = 10.0
    initial_read_timeout_seconds: float = 5.0
    max_jobs: int = 25
    same_host_only: bool = True
    verbose: bool = False
    angry: bool = False
    browser_path: str | None = None
    browser_engine: str = "chromium"
    prefer_open_browser: bool = False
    cdp_endpoint: str | None = None
    cdp_probe_timeout_seconds: float = 5.0
    allow_disallowed_robots: bool = False
    # If True, never skip headful Playwright for job URLs after plain HTTP succeeds once (default: skip for speed).
    headful_for_each_job: bool = False
    # If True, stop after landing-page discovery and do not fetch individual job details.
    discover_only: bool = False
    output_spec: OutputSpec = OutputSpec.none()


class EthicalAvatureScraper:
    def __init__(self, settings: ScrapeSettings) -> None:
        self.settings = settings
        self.robots = RobotsPolicy(
            user_agent=settings.user_agent,
            timeout_seconds=min(settings.timeout_seconds, 15.0),
        )
        fetch_settings = FetchSettings(
            user_agent=settings.user_agent,
            timeout_seconds=settings.timeout_seconds,
            initial_read_timeout_seconds=settings.initial_read_timeout_seconds,
            verbose=settings.verbose,
            browser_path=settings.browser_path,
            browser_engine=settings.browser_engine,
            prefer_open_browser=settings.prefer_open_browser,
            cdp_endpoint=settings.cdp_endpoint,
            cdp_probe_timeout_seconds=settings.cdp_probe_timeout_seconds,
        )
        self.requests_fetcher = RequestsFetcher(fetch_settings)
        self.playwright_headless = PlaywrightFetcher(fetch_settings, headless=True)
        self.playwright_headful = PlaywrightFetcher(fetch_settings, headless=False)
        # Remember lowest fetch tier that worked for job-detail pages; skip earlier tiers on later URLs.
        self._job_detail_fetch_start_index: int = 0
        self._job_detail_session_skip_notice_shown: bool = False

    def run(
        self,
        target_url: str,
        *,
        job_urls: list[str] | None = None,
        cache: ReportCache | None = None,
        cached_report: ScrapeReport | None = None,
    ) -> ScrapeReport:
        target_url = normalize_url(target_url)
        report = ScrapeReport(target_url=target_url, output_fields=self._report_output_fields())
        if cached_report is not None:
            report = cache.merge(report, cached_report) if cache else report

        if report.landing_page and report.landing_page.content:
            landing = report.landing_page
        else:
            landing = None
            tried: list[str] = []
            for candidate in career_landing_url_candidates(target_url):
                tried.append(candidate)
                res, _ = self._guarded_progressive_fetch(candidate, report=report, require_job_page=False)
                report.landing_page = res
                self._write_incremental(cache, report)
                if res and res.ok and res.content:
                    landing = res
                    if candidate != target_url:
                        report.warnings.append(f"Landing page succeeded at {candidate} (seed was {target_url}).")
                    target_url = candidate
                    report.target_url = candidate
                    break
            if landing is None or not landing.content:
                report.warnings.append(
                    "Landing page could not be retrieved with recognizable job content after tries: "
                    + ", ".join(tried)
                    + "."
                )
                self._write_incremental(cache, report)
                return report

        supplied_urls = [normalize_url(url, base_url=target_url) for url in (job_urls or [])]

        if is_linkedin_hosted_careers_landing(landing.content, target_url):
            report.warnings.append(
                "Skipped: landing page lists jobs primarily on LinkedIn; no native Avature job URLs to scrape."
            )
            self._write_incremental(cache, report)
            return report

        discovered_urls = discover_job_urls(
            landing.content,
            target_url,
            same_host_only=self.settings.same_host_only,
        )
        discovered_urls = self._discover_paginated_listing_job_urls(
            target_url,
            landing.content,
            report=report,
            cache=cache,
            initial_urls=discovered_urls,
        )
        report.discovered_job_urls = _dedupe_preserve_order(
            [*supplied_urls, *report.discovered_job_urls, *discovered_urls]
        )[: self.settings.max_jobs]
        self._write_incremental(cache, report)
        if self.settings.discover_only:
            report.warnings.append(
                "Discover-only mode: collected job URLs from landing content; skipped job-detail fetches."
            )
            self._write_incremental(cache, report)
            return report

        if not report.discovered_job_urls:
            if target_url not in {job.url for job in report.jobs}:
                is_job, reason = validate_job_page(landing.content, target_url)
                if is_job:
                    job = extract_job_record(landing.content, target_url)
                    if self.settings.output_spec.includes_ingestion_output():
                        job = apply_runtime_ingestion_fields(job)
                    report.jobs.append(job)
                else:
                    report.warnings.append(f"Target page was not saved as a job: {reason}")
                self._write_incremental(cache, report)
            return report

        seen_job_urls = {job.url for job in report.jobs}
        total_jobs = len(report.discovered_job_urls)
        with start_cat_progress(total_jobs, angry=self.settings.angry) as cat_progress:
            cat_progress.update(0)
            for index, job_url in enumerate(report.discovered_job_urls, start=1):
                if job_url in seen_job_urls:
                    show_job_progress(index, total_jobs, job_url, cached=True, verbose=self.settings.verbose)
                    cat_progress.update(index)
                    continue

                show_job_progress(index, total_jobs, job_url, verbose=self.settings.verbose)
                cat_progress.update(index - 1)
                if not self.settings.angry:
                    polite_delay(self.settings.delay_seconds)
                result, job = self._guarded_progressive_fetch(
                    job_url,
                    report=report,
                    require_job_page=True,
                )
                if result and result.ok and job is not None:
                    report.jobs.append(job)
                    seen_job_urls.add(job_url)
                    self._write_incremental(cache, report)
                elif result is not None:
                    report.warnings.append(f"Skipped non-job or unusable result: {job_url}")
                    self._write_incremental(cache, report)
                cat_progress.update(index)

        return report

    def write_report(self, report: ScrapeReport, path: Path) -> None:
        ReportCache(path).write(report)

    def _write_incremental(self, cache: ReportCache | None, report: ScrapeReport) -> None:
        if cache is None:
            return
        cache.write(report)
        show_cache_write(str(cache.path), len(report.jobs), verbose=self.settings.verbose)

    def _report_output_fields(self) -> list[str]:
        from .models import (
            DEFAULT_JOB_OUTPUT_FIELDS,
            JOBDATAPOOL_OUTPUT_FIELDS,
            OPTION_FLAG_OUTPUT_FIELDS,
        )

        spec = self.settings.output_spec
        if spec.want_all_jobdatapool:
            fields = list(dict.fromkeys([*DEFAULT_JOB_OUTPUT_FIELDS, *JOBDATAPOOL_OUTPUT_FIELDS]))
            return fields

        fields = list(DEFAULT_JOB_OUTPUT_FIELDS)
        if spec.want_skills:
            fields.extend(OPTION_FLAG_OUTPUT_FIELDS["skills"])
        if spec.includes_ingestion_output():
            fields.extend(OPTION_FLAG_OUTPUT_FIELDS["ingestion_date"])
            fields.append("job_posted_date")
        if spec.want_education_requirements:
            fields.extend(OPTION_FLAG_OUTPUT_FIELDS["education_requirements"])
        if spec.want_company_name:
            fields.extend(OPTION_FLAG_OUTPUT_FIELDS["company_name"])
        seen: set[str] = set()
        out: list[str] = []
        for f in fields:
            if f not in seen:
                seen.add(f)
                out.append(f)
        return out

    def _guarded_progressive_fetch(
        self,
        url: str,
        *,
        report: ScrapeReport,
        require_job_page: bool = False,
    ) -> tuple[FetchResult | None, JobSummary | None]:
        decision = self.robots.check(url)
        report.robots.append(decision)
        show_robots(decision, verbose=self.settings.verbose)

        if not decision.allowed:
            if self.settings.allow_disallowed_robots:
                report.warnings.append(
                    "robots.txt disallows this URL for the configured user agent; proceeding anyway because "
                    "--allow-disallowed-robots was set. You are responsible for authorization and compliance."
                )
            elif not self.settings.angry:
                if not require_legal_acknowledgement():
                    console.print("[bold green]Stopped safely. No request was made to the disallowed URL.[/]")
                    report.warnings.append(f"Stopped before fetching disallowed URL: {url}")
                    return None, None
                report.warnings.append(
                    f"Operator explicitly acknowledged legal responsibility before fetching disallowed URL: {url}"
                )
        
        attempts = [
            ("headful Playwright", self.playwright_headful.fetch, "headful Playwright"),
        ]
        if not self.settings.angry:
            attempts.extend([
                ("Requests GET", self.requests_fetcher.fetch, None),
                ("headless Playwright", self.playwright_headless.fetch, "headless Playwright"),
            ])
        start_index = 0
        # After the first job succeeds via plain HTTP, we normally skip headful for speed — but that
        # also skips CDP/Edge entirely. If the operator asked for CDP or prefer-open, always start at
        # headful (tier 0) for every job-detail URL so the browser stays in the loop.
        allow_http_fast_path_for_jobs = (
            require_job_page
            and not self.settings.headful_for_each_job
            and not (self.settings.cdp_endpoint or "").strip()
            and not self.settings.prefer_open_browser
        )
        if allow_http_fast_path_for_jobs:
            start_index = min(self._job_detail_fetch_start_index, len(attempts) - 1)

        last_result: FetchResult | None = None
        spec = self.settings.output_spec
        attempted_tiers: set[int] = set()

        for real_index in range(start_index, len(attempts)):
            status_label, fetch, fallback_label = attempts[real_index]
            if real_index > 0 and (real_index - 1) in attempted_tiers:
                explain_next_fetch(fallback_label or "next fetch method")
            elif real_index == start_index and start_index > 0 and not self._job_detail_session_skip_notice_shown:
                console.print(
                    "[dim]Skipping headful (and earlier) fetch tiers for job-detail URLs: plain HTTP already "
                    "produced a full job row earlier this run — no visible browser for remaining listings "
                    "(use --headful-for-each-job to always open headful first).[/]"
                )
                self._job_detail_session_skip_notice_shown = True

            result = run_with_inference_status(
                status_label,
                self.settings.timeout_seconds,
                lambda fetch=fetch: fetch(url, require_job_page=require_job_page),
            )
            if result.ok and require_job_page:
                is_job, reason = validate_job_page(result.content, url)
                if not is_job:
                    result = result.model_copy(
                        update={
                            "ok": False,
                            "error": f"garbage/non-job data detected: {reason}",
                        }
                    )
            last_result = result
            attempted_tiers.add(real_index)

            if not result.ok:
                show_fetch_attempt(result, verbose=self.settings.verbose)
                continue

            if not require_job_page:
                show_fetch_attempt(result, verbose=self.settings.verbose)
                return result, None

            job = extract_job_record(result.content, url)
            if spec.includes_ingestion_output():
                job = apply_runtime_ingestion_fields(job)

            missing = missing_required_fields(job, spec)
            is_last = real_index == len(attempts) - 1

            if not missing:
                show_fetch_attempt(result, verbose=self.settings.verbose)
                self._note_job_detail_success_tier(real_index, require_job_page=require_job_page)
                return result, job

            if not is_last:
                result = result.model_copy(
                    update={
                        "ok": False,
                        "error": f"missing required fields: {', '.join(missing)}",
                    }
                )
                show_fetch_attempt(result, verbose=self.settings.verbose)
                continue

            if is_last:
                job = apply_headful_inference(job, result.content, url, spec)
                if spec.includes_ingestion_output():
                    job = apply_runtime_ingestion_fields(job)
                    if not (job.job_posted_date or "").strip() and (job.ingest_utc_date or "").strip():
                        job = job.model_copy(
                            update={
                                "job_posted_date": job.ingest_utc_date,
                                "data_quality_warnings": [
                                    *job.data_quality_warnings,
                                    "job_posted_date was not found on the page; copied ingest_utc_date as a placeholder — verify manually.",
                                ],
                            }
                        )

                defaults_missing = missing_default_fields(job)
                if defaults_missing:
                    result = result.model_copy(
                        update={
                            "ok": False,
                            "error": f"after headful fetch, still missing defaults: {', '.join(defaults_missing)}",
                        }
                    )
                    show_fetch_attempt(result, verbose=self.settings.verbose)
                    report.warnings.append(f"All fetch methods failed required columns for job: {url}")
                    return result, None

                opt_missing = missing_optional_page_fields(job, spec)
                if opt_missing:
                    job = job.model_copy(
                        update={
                            "needs_manual_review": True,
                            "data_quality_warnings": [
                                *job.data_quality_warnings,
                                f"Optional requested fields still empty after headful pass: {', '.join(opt_missing)}.",
                            ],
                        }
                    )
                show_fetch_attempt(result, verbose=self.settings.verbose)
                self._note_job_detail_success_tier(real_index, require_job_page=require_job_page)
                return result, job

        report.warnings.append(f"All fetch methods failed to retrieve recognizable job content: {url}")
        return last_result, None

    def _note_job_detail_success_tier(self, tier_index: int, *, require_job_page: bool) -> None:
        if not require_job_page:
            return
        self._job_detail_fetch_start_index = max(self._job_detail_fetch_start_index, tier_index)

    def _discover_paginated_listing_job_urls(
        self,
        base_url: str,
        landing_html: str,
        *,
        report: ScrapeReport,
        cache: ReportCache | None,
        initial_urls: list[str],
    ) -> list[str]:
        signals = extract_listing_discovery_signals(
            landing_html,
            base_url,
            same_host_only=self.settings.same_host_only,
        )
        legend = signals.pagination_legend
        if legend is None:
            return initial_urls
        hints_lower = {h.lower() for h in signals.query_param_hints}
        if "joboffset" not in hints_lower:
            return initial_urls

        parsed = urlparse(base_url)
        params = parse_qs(parsed.query or "")
        try:
            current_offset = int((params.get("jobOffset") or params.get("joboffset") or ["0"])[0])
        except (TypeError, ValueError):
            current_offset = 0
        page_size = max(1, int(legend.page_size))
        known_total = int(legend.total_results)
        discovered = _dedupe_preserve_order(initial_urls)
        max_pages = 20
        empty_pages = 0
        max_empty_pages = 2

        for _ in range(max_pages):
            next_offset = current_offset + page_size
            if next_offset >= known_total:
                break
            if len(discovered) >= self.settings.max_jobs:
                break
            query = parse_qs(parsed.query or "")
            query["jobOffset"] = [str(next_offset)]
            if "jobrecordsperpage" in hints_lower and "jobRecordsPerPage" not in query:
                query["jobRecordsPerPage"] = [str(page_size)]
            page_url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "",
                    urlencode([(k, v) for k, vals in query.items() for v in vals]),
                    "",
                )
            )
            result, _job_unused = self._guarded_progressive_fetch(page_url, report=report, require_job_page=False)
            if not result or not result.ok or not result.content:
                break
            extra = discover_job_urls(
                result.content,
                page_url,
                same_host_only=self.settings.same_host_only,
            )
            before = len(discovered)
            discovered = _dedupe_preserve_order([*discovered, *extra])
            if len(discovered) == before:
                empty_pages += 1
            else:
                empty_pages = 0
            if empty_pages >= max_empty_pages:
                break
            current_offset = next_offset
            self._write_incremental(cache, report)
        return discovered


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output
