"""CSV export and JobPool.live scrape-cache submission (Netlify-backed public cache)."""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .git_identity import resolve_cache_user_key
from .models import JobSummary, ScrapeReport

# Columns aligned with livejobpool `cache-scraped-listings.js` REQUIRED_FIELDS (strings in CSV).
JOBPOOL_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "job_title",
    "company_name",
    "job_location",
    "job_seniority_level",
    "job_employment_type",
    "job_industries",
    "job_summary",
    "job_base_pay_range",
    "job_posted_date",
    "competitiveness_score",
    "skills",
    "certifications",
    "industries",
    "achievements",
    "url",
    "apply_link",
    "country_code",
    "ingest_utc_date",
    "ingest_utc_hour",
    "source_observed_utc",
    "record_lifecycle_state",
    "source_business_url",
)

DEFAULT_JOBPOOL_API_URL = "https://jobpool.live/api/scrape-cache"


def job_to_jobpool_listing(job: JobSummary) -> dict[str, str]:
    """Map a scraped job row to the JobPool cache listing shape (all string values)."""
    hour = job.ingest_utc_hour
    return {
        "id": str(job.id or job.requisition_id or "").strip(),
        "job_title": str(job.job_title or job.title or "").strip(),
        "company_name": str(job.company_name or "").strip(),
        "job_location": str(job.job_location or job.location or "").strip(),
        "job_seniority_level": str(job.job_seniority_level or "").strip(),
        "job_employment_type": str(job.job_employment_type or "").strip(),
        "job_industries": str(job.job_industries or "").strip(),
        "job_summary": str(
            job.job_summary or (job.description or "")[:8000] or job.description_preview or ""
        ).strip(),
        "job_base_pay_range": str(job.job_base_pay_range or "").strip(),
        "job_posted_date": str(job.job_posted_date or "").strip(),
        "competitiveness_score": str(job.competitiveness_score or "").strip(),
        "skills": str(job.skills or "").strip(),
        "certifications": str(job.certifications or "").strip(),
        "industries": str(job.industries or job.job_industries or "").strip(),
        "achievements": str(job.achievements or "").strip(),
        "url": str(job.url or "").strip(),
        "apply_link": str(job.apply_link or "").strip(),
        "country_code": str(job.country_code or "").strip(),
        "ingest_utc_date": str(job.ingest_utc_date or "").strip(),
        "ingest_utc_hour": "" if hour is None else str(int(hour)),
        "source_observed_utc": "",
        "record_lifecycle_state": "",
        "source_business_url": str(job.source_business_url or "").strip(),
    }


def write_jobpool_csv(path: Path, jobs: list[JobSummary]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(JOBPOOL_CSV_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for job in jobs:
            row = job_to_jobpool_listing(job)
            writer.writerow({k: row.get(k, "") for k in JOBPOOL_CSV_COLUMNS})
    return path


def build_upload_payload(
    report: ScrapeReport,
    *,
    user_name: str | None = None,
) -> dict[str, Any]:
    user = (user_name or "").strip() or resolve_cache_user_key()
    listings = [job_to_jobpool_listing(j) for j in report.jobs]
    source = ""
    if report.jobs:
        source = (report.jobs[0].source_business_url or "").strip()
    if not source:
        try:
            from .urls import normalize_url

            parsed = urlparse(normalize_url(report.target_url))
            source = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            source = report.target_url
    return {
        "user_name": user,
        "username": user,
        "request_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_business_url": source,
        "source_business_urls": [source] if source else [],
        "listings": listings,
    }


def append_local_jobpool_mirror(payload: dict[str, Any]) -> Path | None:
    """Append the same JSON body as the POST to a local file under LIVEJOBPOOL_ROOT (optional)."""
    root = (os.environ.get("LIVEJOBPOOL_ROOT") or "").strip()
    if not root:
        return None
    user = str(payload.get("user_name") or "unknown").strip() or "unknown"
    safe = re_slug_filename(user)
    cache_dir = Path(root).expanduser() / ".aventure-scraper-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{safe}.jsonl"
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return path


def re_slug_filename(user: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", user.strip())[:120]
    return s or "unknown"


def post_jobpool_scrape_cache(payload: dict[str, Any], *, timeout: float = 60.0) -> tuple[bool, str]:
    url = (os.environ.get("JOBPOOL_SCRAPE_CACHE_URL") or DEFAULT_JOBPOOL_API_URL).strip()
    try:
        r = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "aventure-scraper/0.2.4 (+https://jobpool.live)",
            },
            timeout=timeout,
        )
        if r.ok:
            return True, f"HTTP {r.status_code}: {r.text[:500]}"
        return False, f"HTTP {r.status_code}: {r.text[:800]}"
    except requests.RequestException as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def export_csv_and_maybe_upload(
    report: ScrapeReport,
    csv_path: Path,
    *,
    upload: bool,
    user_name: str | None = None,
) -> tuple[Path | None, bool | None, str]:
    """Write CSV; optionally POST to JobPool. Local JSONL mirror appends when LIVEJOBPOOL_ROOT is set.

    Returns (csv_path, http_ok_or_none, combined_status_message).
    """
    if not report.jobs:
        return None, None, "no jobs to export"
    written = write_jobpool_csv(csv_path, report.jobs)
    payload = build_upload_payload(report, user_name=user_name)
    mirror_path = append_local_jobpool_mirror(payload)
    bits: list[str] = []
    if mirror_path:
        bits.append(f"local mirror → {mirror_path}")
    if not upload:
        return written, None, "; ".join(bits) if bits else "CSV written"
    ok, detail = post_jobpool_scrape_cache(payload)
    bits.insert(0, detail)
    return written, ok, "; ".join(bits)
