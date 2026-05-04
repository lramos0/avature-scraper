from avature_ethics_scraper.cache import ReportCache
from avature_ethics_scraper.extract import (
    discover_job_urls,
    extract_job_summary,
    score_job_listing_content,
    validate_job_page,
)
from avature_ethics_scraper.models import FetchMethod, FetchResult, JobSummary, ScrapeReport


def test_discovery_rejects_save_job_links():
    html = '''
    <a href="/careers/JobDetail/Senior-Software-Engineer/18957">real job</a>
    <a href="/careers/SaveJob?jobId=18957">save job</a>
    '''
    urls = discover_job_urls(html, "https://bloomberg.avature.net/careers")
    assert urls == ["https://bloomberg.avature.net/careers/JobDetail/Senior-Software-Engineer/18957"]


def test_validate_job_page_rejects_registration_garbage():
    html = """
    <html><head><title>Save Job Registration | Bloomberg</title></head>
    <body>Bloomberg Our Company Events Search Jobs Login Do you have an account? Log in
    SAVE APPLICATION REGISTRATION Automated Source Picker (hidden) First Name * Last name * Email (Username) *
    Password Your password must: Have at least 8 characters. Password confirmation *</body></html>
    """
    ok, reason = validate_job_page(html, "https://bloomberg.avature.net/careers/SaveJob?jobId=18957")
    assert not ok
    assert "SaveJob" in reason or "registration" in reason


def test_validate_job_page_accepts_realistic_job_detail():
    html = """
    <html><head><title>Senior Software Engineer | Bloomberg</title>
    <script type="application/ld+json">{"@type":"JobPosting","title":"Senior Software Engineer","datePosted":"2026-01-01","employmentType":"FULL_TIME"}</script>
    </head><body><h1>Senior Software Engineer</h1><div>Location: New York</div>
    <section>Job description</section><p>Responsibilities include building reliable systems, collaborating with product teams,
    improving platform performance, writing tested Python services, supporting production systems, and mentoring engineers.
    Qualifications include professional software engineering experience, distributed systems knowledge, strong communication,
    and experience with cloud infrastructure, observability, CI/CD, and secure development practices.</p>
    <a>Apply Now</a></body></html>
    """
    ok, reason = validate_job_page(html, "https://bloomberg.avature.net/careers/JobDetail/Senior-Software-Engineer/18957")
    assert ok, reason


def test_score_rejects_registration_shell_even_with_job_words():
    html = """
    <html><head><title>Save Job Registration | Bloomberg</title></head>
    <body><h1>Bloomberg</h1><p>Job description responsibilities qualifications apply now.</p>
    <p>Save Application Registration. Do you have an account? Log in. First Name * Last name *
    Password confirmation. Your password must: Have at least 8 characters.</p>
    <p>Extra page filler so this is not rejected only because it is tiny. Extra page filler so this is not
    rejected only because it is tiny. Extra page filler so this is not rejected only because it is tiny.</p>
    </body></html>
    """
    score = score_job_listing_content(html, "https://bloomberg.avature.net/careers/SaveJob?jobId=18957")
    assert score.score < 4
    assert any("registration" in reason for reason in score.reasons)

def test_extract_title_unescapes_entities_and_preserves_internal_hyphen():
    html = """
    <html><head><title>Senior Software Engineer - AI Inference - 18957 - Bloomberg</title></head>
    <body><h1>Senior Software Engineer - AI Inference</h1><p>Job Description Responsibilities Qualifications Apply Now</p></body></html>
    """
    job = extract_job_summary(html, "https://example.avature.net/careers/JobDetail/Senior-Software-Engineer-AI-Inference/18957")
    assert job.title == "Senior Software Engineer - AI Inference"


def test_extract_description_preview_strips_markup_and_ui_noise():
    html = """
    <html><head><title>Data Engineer | Example</title><style>.hidden{display:none}</style></head>
    <body><nav>Search jobs</nav><h1>Data Engineer</h1>
    <section><p>Job description <strong>Build</strong> ETL &amp; APIs.</p></section>
    <script>alert("x")</script><form>First Name * Last Name * Password confirmation</form></body></html>
    """
    job = extract_job_summary(html, "https://example.avature.net/careers/JobDetail/Data-Engineer/12345")
    assert job.description_preview is not None
    assert "Job description Build ETL & APIs." in job.description_preview
    assert "<strong>" not in job.description_preview
    assert "alert" not in job.description_preview
    assert "First Name" not in job.description_preview


def test_output_cache_does_not_persist_raw_html(tmp_path):
    report = ScrapeReport(
        target_url="https://example.avature.net/careers",
        landing_page=FetchResult(
            url="https://example.avature.net/careers",
            method=FetchMethod.REQUESTS,
            ok=True,
            status_code=200,
            content="<html>very large raw page</html>",
        ),
    )
    path = tmp_path / "jobs.json"
    ReportCache(path).write(report)
    saved = path.read_text()
    assert "very large raw page" not in saved
    loaded = ReportCache(path).load()
    assert loaded.landing_page is not None
    assert loaded.landing_page.content == ""


def test_output_cache_removes_bogus_jobs_and_related_urls(tmp_path):
    real_url = "https://bloomberg.avature.net/careers/JobDetail/Senior-Software-Engineer/18957"
    bogus_job_url = "https://bloomberg.avature.net/careers/JobDetail/Bloomberg/99999"
    save_job_url = "https://bloomberg.avature.net/careers/SaveJob?jobId=99999"
    report = ScrapeReport(
        target_url="https://bloomberg.avature.net/careers",
        discovered_job_urls=[real_url, bogus_job_url, save_job_url],
        jobs=[
            JobSummary(
                url=bogus_job_url,
                title="Bloomberg",
                description_preview="Save Application Registration Password confirmation",
            ),
            JobSummary(
                url=real_url,
                title="Senior Software Engineer",
                description_preview="<p>Job description <strong>Build</strong> APIs &amp; data systems.</p>",
            ),
        ],
    )
    path = tmp_path / "jobs.json"
    cache = ReportCache(path)

    cache.write(report)
    saved = path.read_text()
    loaded = cache.load()

    assert bogus_job_url not in saved
    assert save_job_url not in saved
    assert "<strong>" not in saved
    assert loaded.discovered_job_urls == [real_url]
    assert [job.url for job in loaded.jobs] == [real_url]
    assert loaded.jobs[0].description_preview == "Job description Build APIs & data systems."


def test_requisition_id_prefers_url_id_not_requirements_word():
    html = """
    <html><head><title>Example Engineer - 18957 - Bloomberg</title></head>
    <body><h1>Example Engineer</h1><p>Requirements: Python</p><p>Qualifications apply now</p></body></html>
    """
    from avature_ethics_scraper.extract import extract_job_summary
    job = extract_job_summary(html, "https://bloomberg.avature.net/careers/JobDetail/Example-Engineer/18957")
    assert job.requisition_id == "18957"


def test_title_unescapes_html_entities():
    html = """
    <html><head><title>Analytics &amp; Sales - 18811 - Bloomberg</title></head>
    <body><h1>Analytics &amp; Sales</h1><p>Job description responsibilities qualifications apply now</p></body></html>
    """
    from avature_ethics_scraper.extract import extract_job_summary
    job = extract_job_summary(html, "https://bloomberg.avature.net/careers/JobDetail/Analytics-Sales/18811")
    assert job.title == "Analytics & Sales"
