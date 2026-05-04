from avature_ethics_scraper.csv_jobpool import job_to_jobpool_listing, write_jobpool_csv
from avature_ethics_scraper.models import JobSummary


def test_job_to_jobpool_listing_maps_core_fields():
    job = JobSummary(
        id="99",
        url="https://example.avature.net/careers/JobDetail/Role/99",
        title="Role",
        job_title="Role",
        company_name="Example Co",
        location="NYC",
        job_location="NYC",
        skills="Python",
        ingest_utc_date="2026-05-01",
        ingest_utc_hour=12,
        source_business_url="https://example.avature.net",
    )
    row = job_to_jobpool_listing(job)
    assert row["id"] == "99"
    assert row["job_title"] == "Role"
    assert row["company_name"] == "Example Co"
    assert row["job_location"] == "NYC"
    assert row["skills"] == "Python"
    assert row["ingest_utc_hour"] == "12"
    assert "source_observed_utc" in row


def test_write_jobpool_csv_roundtrip_header(tmp_path):
    job = JobSummary(
        id="1",
        url="https://x/J/1",
        title="T",
        description="x" * 120,
        location="L",
        company_name="C",
        job_title="T",
        job_location="L",
        ingest_utc_date="2026-01-01",
        ingest_utc_hour=0,
    )
    path = tmp_path / "out.csv"
    write_jobpool_csv(path, [job])
    text = path.read_text(encoding="utf-8-sig")
    assert "id" in text.splitlines()[0]
    assert "1" in text
