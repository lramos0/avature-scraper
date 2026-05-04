"""Incremental output cache for scraper reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from .extract import clean_description_text, is_bogus_job_summary, is_bogus_job_url
from .models import JobSummary, ScrapeReport


class ReportCacheError(RuntimeError):
    """Raised when an existing cache cannot be read safely."""


class ReportCache:
    """Small JSON-backed cache that doubles as the final report.

    The cache is intentionally the same document the user asked for via
    ``--output``. There is no separate resume flag or hidden state file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def exists(self) -> bool:
        return self.path.exists() and self.path.is_file()

    def load(self) -> ScrapeReport:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return _sanitize_loaded_report(ScrapeReport.model_validate(raw))
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ReportCacheError(f"Could not read existing output cache: {self.path}") from exc

    def write(self, report: ScrapeReport) -> None:
        """Atomically write the user-facing report/cache.

        The output file doubles as the seen-job cache, but it should not become a
        giant HTML dump. Full response bodies are useful inside a single run for
        parsing and fallback decisions; once persisted, the cache only needs URLs,
        status, method, jobs, robots decisions, and warnings.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        persisted = _strip_raw_fetch_content(_remove_bogus_cached_data(report))
        tmp_path.write_text(json.dumps(persisted.model_dump(mode="json"), indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def merge(self, fresh: ScrapeReport, cached: ScrapeReport) -> ScrapeReport:
        """Merge reusable cached data into a fresh run report."""
        if cached.target_url == fresh.target_url:
            cached = _remove_bogus_cached_data(cached)
            # Do not reuse cached landing-page bodies. Older versions persisted
            # raw HTML, and even stripped entries do not give us fresh discovery.
            fresh.discovered_job_urls = _dedupe([*cached.discovered_job_urls, *fresh.discovered_job_urls])
            fresh.jobs = _dedupe_jobs(_filter_cached_jobs(cached.jobs))
            fresh.warnings = _dedupe([*cached.warnings, *fresh.warnings])
        return fresh


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _dedupe_jobs(jobs: Iterable[JobSummary]) -> list[JobSummary]:
    seen: set[str] = set()
    out: list[JobSummary] = []
    for job in jobs:
        if job.url not in seen:
            seen.add(job.url)
            out.append(job)
    return out



def _sanitize_loaded_report(report: ScrapeReport) -> ScrapeReport:
    """Normalize old output files before using them as a cache."""
    cleaned = _remove_bogus_cached_data(report)
    if cleaned.landing_page is not None:
        cleaned.landing_page = cleaned.landing_page.model_copy(update={"content": ""})
    return cleaned


def _filter_cached_jobs(jobs: Iterable[JobSummary]) -> list[JobSummary]:
    return [_clean_cached_job(job) for job in jobs if not is_bogus_job_summary(job)]


def _remove_bogus_cached_data(report: ScrapeReport) -> ScrapeReport:
    cleaned = report.model_copy(deep=True)
    bogus_job_urls = {job.url for job in cleaned.jobs if is_bogus_job_summary(job)}
    cleaned.jobs = _dedupe_jobs(_filter_cached_jobs(cleaned.jobs))
    cleaned.discovered_job_urls = _dedupe(
        url
        for url in cleaned.discovered_job_urls
        if url not in bogus_job_urls and not is_bogus_job_url(url)
    )
    return cleaned


def _clean_cached_job(job: JobSummary) -> JobSummary:
    return job.model_copy(
        update={
            "description_preview": clean_description_text(job.description_preview),
            "description": clean_description_text(job.description),
        }
    )

def _strip_raw_fetch_content(report: ScrapeReport) -> ScrapeReport:
    stripped = report.model_copy(deep=True)
    if stripped.landing_page is not None:
        stripped.landing_page = stripped.landing_page.model_copy(update={"content": ""})
    return stripped
