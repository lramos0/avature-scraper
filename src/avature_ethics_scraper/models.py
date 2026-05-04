"""Typed models used by the scraper."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_JOB_OUTPUT_FIELDS = ("id", "title", "description", "location", "url")

JOBDATAPOOL_OUTPUT_FIELDS = (
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
    "source_business_url",
    "education_requirements",
)

OPTION_FLAG_OUTPUT_FIELDS = {
    "skills": ("skills",),
    "ingestion_date": ("ingest_utc_date", "ingest_utc_hour"),
    "education_requirements": ("education_requirements",),
    "company_name": ("company_name",),
}


class FetchMethod(str, Enum):
    REQUESTS = "requests"
    PLAYWRIGHT_HEADLESS = "playwright-headless"
    PLAYWRIGHT_HEADFUL = "playwright-headful"


class RobotsDecision(BaseModel):
    url: str
    robots_url: str
    allowed: bool
    has_robots_txt: bool
    reason: str
    user_agent: str


class FetchResult(BaseModel):
    url: str
    method: FetchMethod
    ok: bool
    status_code: int | None = None
    content: str = ""
    content_type: str | None = None
    error: str | None = None


class JobSummary(BaseModel):
    id: str | None = None
    title: str | None = None
    description: str | None = None
    location: str | None = None
    url: str
    job_title: str | None = None
    company_name: str | None = None
    job_location: str | None = None
    job_seniority_level: str | None = None
    job_employment_type: str | None = None
    job_industries: str | None = None
    job_summary: str | None = None
    job_base_pay_range: str | None = None
    job_posted_date: str | None = None
    competitiveness_score: str | None = None
    skills: str | None = None
    certifications: str | None = None
    industries: str | None = None
    achievements: str | None = None
    apply_link: str | None = None
    country_code: str | None = None
    ingest_utc_date: str | None = None
    ingest_utc_hour: int | None = None
    source_business_url: str | None = None
    education_requirements: str | None = None
    requisition_id: str | None = None
    description_preview: str | None = None
    description_truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    needs_manual_review: bool = False
    data_quality_warnings: list[str] = Field(default_factory=list)


class ScrapeReport(BaseModel):
    target_url: str
    output_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_JOB_OUTPUT_FIELDS))
    robots: list[RobotsDecision] = Field(default_factory=list)
    landing_page: FetchResult | None = None
    discovered_job_urls: list[str] = Field(default_factory=list)
    jobs: list[JobSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
