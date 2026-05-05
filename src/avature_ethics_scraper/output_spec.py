"""Which job columns are required (defaults + CLI-requested) and field completeness checks."""

from __future__ import annotations

from dataclasses import dataclass

import os

from .models import JobSummary
from .urls import stable_id_from_url

# Full cleaned description stored in JSON; cap incremental cache growth.
MAX_JOB_DESCRIPTION_CHARS = 200_000
MIN_DEFAULT_DESCRIPTION_LEN = 80


@dataclass(frozen=True)
class OutputSpec:
    """Optional columns the operator asked to collect (CLI flags)."""

    want_skills: bool = False
    want_ingestion_date: bool = False
    want_education_requirements: bool = False
    want_company_name: bool = False
    want_all_jobdatapool: bool = False

    @classmethod
    def none(cls) -> OutputSpec:
        return cls()

    @classmethod
    def all_jobdatapool(cls) -> OutputSpec:
        """Enable every optional gate plus full JobDataPool-shaped export columns."""
        return cls(
            want_skills=True,
            want_ingestion_date=True,
            want_education_requirements=True,
            want_company_name=True,
            want_all_jobdatapool=True,
        )

    def page_driven_option_keys(self) -> frozenset[str]:
        """Keys that must be satisfied from HTML/metadata before the final fetch tier."""
        keys: set[str] = set()
        skills = self.want_skills or self.want_all_jobdatapool
        ingest = self.want_ingestion_date or self.want_all_jobdatapool
        edu = self.want_education_requirements or self.want_all_jobdatapool
        company = self.want_company_name or self.want_all_jobdatapool
        if skills:
            keys.add("skills")
        if ingest:
            keys.add("job_posted_date")
        if edu:
            keys.add("education_requirements")
        if company:
            keys.add("company_name")
        return frozenset(keys)

    def includes_ingestion_output(self) -> bool:
        return self.want_ingestion_date or self.want_all_jobdatapool


def _stable_id(job: JobSummary) -> str | None:
    rid = (job.requisition_id or "").strip()
    if rid:
        return rid
    url = (job.url or "").strip()
    if not url:
        return None
    return stable_id_from_url(url)


def _default_description(job: JobSummary) -> str | None:
    text = (job.description or "").strip() or (job.description_preview or "").strip()
    return text or None


def missing_default_fields(job: JobSummary) -> list[str]:
    # Many Avature variants omit a visible "location" string while still having a valid job posting.
    # For batch scraping, allow operator to relax this gate.
    relax_location = bool(os.environ.get("AVATURE_RELAX_LOCATION"))
    missing: list[str] = []
    sid = _stable_id(job)
    if not sid:
        missing.append("id")
    if not (job.title or "").strip():
        missing.append("title")
    desc = _default_description(job)
    if not desc or len(desc) < MIN_DEFAULT_DESCRIPTION_LEN:
        missing.append("description")
    if not relax_location:
        if not (job.location or "").strip():
            missing.append("location")
    if not (job.url or "").strip():
        missing.append("url")
    return missing


def _field_nonempty(job: JobSummary, field: str) -> bool:
    val = getattr(job, field, None)
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, int):
        return True
    return bool(val)


def missing_optional_page_fields(job: JobSummary, spec: OutputSpec) -> list[str]:
    missing: list[str] = []
    skills = spec.want_skills or spec.want_all_jobdatapool
    ingest = spec.want_ingestion_date or spec.want_all_jobdatapool
    edu = spec.want_education_requirements or spec.want_all_jobdatapool
    company = spec.want_company_name or spec.want_all_jobdatapool
    if skills and not _field_nonempty(job, "skills"):
        missing.append("skills")
    if ingest and not _field_nonempty(job, "job_posted_date"):
        missing.append("job_posted_date")
    if edu and not _field_nonempty(job, "education_requirements"):
        missing.append("education_requirements")
    if company and not _field_nonempty(job, "company_name"):
        missing.append("company_name")
    return missing


def missing_required_fields(job: JobSummary, spec: OutputSpec) -> list[str]:
    _ = spec
    # "Required" means the core row shape only. Optional/all-jobdatapool fields are
    # best-effort and should not cause row rejection.
    return missing_default_fields(job)


def apply_runtime_ingestion_fields(job: JobSummary) -> JobSummary:
    """Set ingest_utc_date / ingest_utc_hour from current UTC (when ingestion output is requested)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return job.model_copy(
        update={
            "ingest_utc_date": now.date().isoformat(),
            "ingest_utc_hour": now.hour,
        }
    )
