"""HTML extraction for likely Avature job pages."""

from __future__ import annotations

import html as html_lib
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .models import JobSummary
from .output_spec import MAX_JOB_DESCRIPTION_CHARS, OutputSpec
from .urls import normalize_url, same_host, stable_id_from_url

_JOB_PATH_RE = re.compile(
    r"(?:jobdetail|job-detail|folderdetail|jobs?/detail|requisition|career|careers|positions?|openings?)",
    re.IGNORECASE,
)
_REQ_LABEL_RE = re.compile(r"\b(?:req(?:uisition)?(?:\s+id)?|job\s+id|job\s+number)[\s#:-]+([A-Z0-9_-]{3,})\b", re.IGNORECASE)
_URL_JOB_ID_RE = re.compile(r"/(\d{3,})(?:[/?#]|$)")
_FOLD_DETAIL_RE = re.compile(r"/FolderDetail/", re.I)
_LOCATION_FROM_TEXT_RE = re.compile(
    r"(?:^|[\n\r]|\b)\s*Location\s*[:\s]\s*(.+?)(?=\n|\r|$|(?:\b(?:Work\s+Type|Department|Salary|Apply)\b))",
    re.IGNORECASE | re.MULTILINE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"\b(?:20\d{2}|19\d{2})[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])\b")
_PAY_RE = re.compile(
    r"(?:(?:USD|US\$|\$)\s?\d[\d,]*(?:\.\d{2})?(?:\s?(?:-|to|through)\s?(?:USD|US\$|\$)?\s?\d[\d,]*(?:\.\d{2})?)?(?:\s?(?:per|/)\s?(?:hour|hr|year|yr|annum))?)",
    re.IGNORECASE,
)
_EDUCATION_RE = re.compile(
    r"\b(?:high school diploma|ged|associate(?:'s)? degree|bachelor(?:'s)? degree|master(?:'s)? degree|mba|ph\.?d\.?|doctorate|degree in [A-Za-z ,/&+-]+)\b",
    re.IGNORECASE,
)
_CERTIFICATION_RE = re.compile(
    r"\b(?:CPA|CFA|PMP|CISSP|CISA|CISM|SHRM-CP|SHRM-SCP|SPHR|PHR|CCNA|CCNP|AWS Certified [A-Za-z ]+|Azure [A-Za-z ]+ Associate|Google Cloud [A-Za-z ]+|OSHA\s?\d{2}|RN|LPN)\b",
    re.IGNORECASE,
)

_SKILL_KEYWORDS = (
    "accessibility",
    "agile",
    "airflow",
    "analytics",
    "api",
    "aws",
    "azure",
    "bash",
    "c#",
    "c++",
    "ci/cd",
    "cloud",
    "communication",
    "css",
    "data analysis",
    "data engineering",
    "docker",
    "excel",
    "fastapi",
    "finance",
    "git",
    "go",
    "graphql",
    "html",
    "java",
    "javascript",
    "jira",
    "kubernetes",
    "linux",
    "machine learning",
    "marketing",
    "mentoring",
    "node",
    "observability",
    "power bi",
    "product management",
    "project management",
    "python",
    "react",
    "rest",
    "ruby",
    "sales",
    "salesforce",
    "security",
    "snowflake",
    "spark",
    "sql",
    "tableau",
    "typescript",
)

_COUNTRY_NAME_TO_CODE = {
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "us": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "uk": "GB",
    "england": "GB",
    "ireland": "IE",
    "germany": "DE",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "australia": "AU",
    "new zealand": "NZ",
    "india": "IN",
    "japan": "JP",
    "singapore": "SG",
    "mexico": "MX",
    "brazil": "BR",
}


@dataclass(frozen=True)
class JobContentScore:
    score: int
    reasons: tuple[str, ...]


def discover_job_urls(html: str, base_url: str, *, same_host_only: bool = True) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    discovered: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            absolute = normalize_url(href, base_url=base_url)
        except ValueError:
            continue
        if same_host_only and not same_host(absolute, base_url):
            continue
        if _looks_like_job_url(absolute, anchor.get_text(" ", strip=True)):
            discovered.add(absolute)

    for match in re.finditer(r"https?://[^\s'\"<>]+", html):
        try:
            absolute = normalize_url(match.group(0), base_url=base_url)
        except ValueError:
            continue
        if same_host_only and not same_host(absolute, base_url):
            continue
        if _looks_like_job_url(absolute, ""):
            discovered.add(absolute)

    return sorted(discovered)


def extract_job_summary(html: str, url: str) -> JobSummary:
    """Backward-compatible alias for :func:`extract_job_record`."""
    return extract_job_record(html, url)


_DESCRIPTION_ANCHORS = (
    "\n\nDescription & Requirements",
    "\nDescription & Requirements",
    "\r\n\r\nDescription & Requirements",
    "Description & Requirements",
    "\n\nResponsibilities",
    "\nResponsibilities",
)


def _avature_banner_window(raw_visible: str) -> str:
    """Visible job pages often collapse to one line; cut before long body markers."""
    t = raw_visible.strip()
    if not t:
        return ""
    for anchor in _DESCRIPTION_ANCHORS:
        i = t.find(anchor)
        if i != -1:
            return t[:i].strip()
    return t[:8000] if len(t) > 8000 else t


_REF_NUM_RE = re.compile(
    r"(?:\b(?:[Rr]ef(?:erence)?|[Rr]eq(?:uisition)?)\s*#|[Rr]eq\.)\s*([A-Z0-9][A-Z0-9_-]{2,})\b",
)


def parse_avature_inline_job_header(raw_visible: str | None) -> dict[str, Any]:
    """Parse Avature's inline job summary (often one long line) into structured fields.

    Typical patterns::

        Company, Role Title Location Sydney Business Area Sales … Ref # 10050768
        Role Title - Contract Location New York Business Area News … Ref # 10051267 Description & Requirements …

    ``_description_text`` often joins nodes with spaces, so ``Ref #`` is usually **not** at end-of-string.

    Returns keys: company_name, job_title, location, business_area, requisition_id, stripped_description.
    """
    empty: dict[str, Any] = {
        "company_name": None,
        "job_title": None,
        "location": None,
        "business_area": None,
        "requisition_id": None,
        "stripped_description": None,
    }
    if not raw_visible or not raw_visible.strip():
        return empty

    raw = raw_visible.strip()
    window = _avature_banner_window(raw)
    if len(window) < 25:
        return empty

    ref_m = _REF_NUM_RE.search(window)
    if not ref_m:
        return empty
    ref = ref_m.group(1).strip()
    before_ref = window[: ref_m.start()].rstrip()
    if not before_ref:
        return empty

    location: str | None = None
    business: str | None = None
    head: str | None = None

    m_ba = re.search(
        r"\b[Ll]ocation\s+(.+?)\s+[Bb]usiness\s+[Aa]reas?\s+(.+)$",
        before_ref,
        re.DOTALL,
    )
    if m_ba:
        location = _clean_text(m_ba.group(1))
        business = _clean_text(m_ba.group(2))
        head = _clean_text(before_ref[: m_ba.start()])
    else:
        m_loc = re.search(r"\b[Ll]ocation\s+(.+)$", before_ref, re.DOTALL)
        if not m_loc:
            return empty
        location = _clean_text(m_loc.group(1))
        head = _clean_text(before_ref[: m_loc.start()])

    company: str | None = None
    job_title: str | None = None
    if head and "," in head:
        left, right = head.split(",", 1)
        company = _clean_text(left)
        job_title = _clean_title(_clean_text(right))
    elif head:
        job_title = _clean_title(head)

    ref_span = ref_m.group(0)
    idx = raw.find(ref_span)
    stripped: str | None = None
    if idx != -1:
        tail = raw[idx + len(ref_span) :].strip()
        if tail.lower().startswith("description & requirements"):
            tail = tail[len("Description & Requirements") :].lstrip()
            if tail.startswith(":"):
                tail = tail[1:].lstrip()
        if tail:
            stripped = tail

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if stripped is None and len(lines) >= 2:
        stripped = "\n".join(lines[1:]).strip() or None

    return {
        "company_name": company,
        "job_title": job_title,
        "location": location,
        "business_area": business,
        "requisition_id": ref,
        "stripped_description": stripped,
    }


def _location_from_folder_detail_page(soup: BeautifulSoup, visible_text: str, url: str) -> str | None:
    """Extra location signals for Avature FolderDetail / Epic-style postings (often sparse vs JobDetail)."""
    if not _FOLD_DETAIL_RE.search(url or ""):
        return None
    for sel in (
        "[id*='location' i]",
        "[id*='Location']",
        "[data-automation-id*='location' i]",
        "[class*='location' i]",
    ):
        try:
            t = _selector_text(soup, sel)
        except ValueError:
            continue
        if t and len(t) < 200 and not t.lower().startswith("http"):
            return t
    m = _LOCATION_FROM_TEXT_RE.search(visible_text or "")
    if m:
        cand = _clean_text(m.group(1))
        if cand and len(cand) < 200:
            return cand
    # Inline "Location …" without newline (single-line or paragraph).
    m2 = re.search(r"\bLocation\s*[:\s]\s*([^\n\r]{2,120})", visible_text or "", re.I)
    if m2:
        return _clean_text(m2.group(1))
    return None


def _fallback_location_when_missing(url: str) -> tuple[str, str | None]:
    """Guarantee a non-empty location for default output when the page truly omits it."""
    if _FOLD_DETAIL_RE.search(url or ""):
        text = "Not specified on posting"
        note = (
            "Location was not found on this FolderDetail page; defaulted to 'Not specified on posting' "
            "so the row meets required columns — verify manually or enrich from the source."
        )
        return text, note
    return "", None


def _job_title_from_avature_detail_url(url: str) -> str | None:
    """Derive a human title from /JobDetail|FolderDetail/Slug-Here/12345 URLs."""
    m = re.search(r"/(?:JobDetail|job-detail|FolderDetail)/([^/]+)/(\d{3,})(?:[/?#]|$)", url, re.I)
    if not m:
        return None
    slug = m.group(1).replace("-", " ")
    slug = _WHITESPACE_RE.sub(" ", slug).strip()
    if len(slug) < 3:
        return None
    return _clean_title(slug)


def extract_job_record(html: str, url: str) -> JobSummary:
    soup = BeautifulSoup(html, "html.parser")
    title_dom = _first_non_empty(
        _selector_text(soup, "h1"),
        _selector_text(soup, "[data-automation-id*='jobTitle' i]"),
        _meta_content(soup, "og:title"),
        _selector_text(soup, "title"),
    )
    title_dom = _clean_title(title_dom)
    if title_dom and title_dom.strip().lower() in _COMPANY_ONLY_TITLES:
        title_dom = None

    location = _first_non_empty(
        _meta_content(soup, "job:location"),
        _selector_text(soup, "[data-automation-id*='location' i]"),
        _find_labeled_value(soup, ("location", "locations")),
        _job_location_from_ld(soup),
    )

    metadata = _structured_job_metadata(soup)
    ld_flat = _jobposting_flat_from_page(soup, url)
    body_text = _description_text(soup)
    av = parse_avature_inline_job_header(body_text)
    raw_lines = [ln.strip() for ln in (body_text or "").strip().splitlines() if ln.strip()]

    requisition_id = _first_non_empty(av.get("requisition_id"), _extract_requisition_id(body_text or "", url))

    description_truncated = False
    full_desc = body_text or ""
    dq_notes: list[str] = []
    stripped = av.get("stripped_description")
    if stripped:
        full_desc = str(stripped).strip()
        dq_notes.append(
            "Avature inline summary (title, location, business area, ref) parsed from description text; "
            "banner prefix removed from stored description."
        )
    elif (
        len(raw_lines) == 1
        and (av.get("requisition_id") or "").strip()
        and (av.get("company_name") or av.get("job_title"))
    ):
        full_desc = ""
        dq_notes.append(
            "Avature lead summary matched the entire visible description line; structured fields filled from it."
        )
    if len(full_desc) > MAX_JOB_DESCRIPTION_CHARS:
        full_desc = full_desc[:MAX_JOB_DESCRIPTION_CHARS]
        description_truncated = True

    description_preview = full_desc[:500] if full_desc else None
    stable_id = (requisition_id or "").strip() or stable_id_from_url(url)

    company_name = _first_non_empty(
        av.get("company_name"),
        ld_flat.get("company_name"),
        _meta_content(soup, "og:site_name"),
        _company_from_title_brand(title_dom),
    )

    title = _first_non_empty(
        av.get("job_title"),
        title_dom,
        _job_title_from_avature_detail_url(url),
    )
    title = _clean_title(title)

    location = _first_non_empty(
        av.get("location"),
        location,
        _location_from_folder_detail_page(soup, body_text or "", url),
        _location_from_folder_detail_page(soup, full_desc or "", url),
    )

    if not (location or "").strip():
        fb_text, fb_note = _fallback_location_when_missing(url)
        if fb_text:
            location = fb_text
            if fb_note:
                dq_notes.append(fb_note)

    job_posted_date = _first_non_empty(
        ld_flat.get("job_posted_date"),
        metadata.get("datePosted"),
        _find_labeled_value(soup, ("posted", "date posted", "posting date")),
    )

    skills_text = _first_non_empty(
        ld_flat.get("skills"),
        _skills_from_ld_list(soup),
        _infer_skills_from_text(full_desc),
    )

    education_requirements = _first_non_empty(
        ld_flat.get("education_requirements"),
        _find_labeled_value(soup, ("education", "education requirements", "minimum education", "degree")),
        _education_snippets_from_text(full_desc),
    )

    apply_link = _first_non_empty(ld_flat.get("apply_link"), _find_apply_href(soup, url))
    base_pay = ld_flat.get("job_base_pay_range")
    seniority = ld_flat.get("job_seniority_level")
    emp_type = _first_non_empty(ld_flat.get("job_employment_type"), metadata.get("employmentType"))
    industries = _first_non_empty(
        av.get("business_area"),
        ld_flat.get("job_industries"),
        ld_flat.get("industries"),
    )
    certifications = _certifications_from_text(full_desc)
    country_code = ld_flat.get("country_code")
    parsed = urlparse(url)
    source_business_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None

    job_summary_text = _first_non_empty(ld_flat.get("job_summary"), description_preview)

    return JobSummary(
        id=stable_id,
        title=title,
        job_title=title,
        description=full_desc if full_desc else None,
        description_preview=description_preview,
        description_truncated=description_truncated,
        location=location,
        job_location=location,
        url=url,
        company_name=company_name,
        job_posted_date=job_posted_date,
        skills=skills_text,
        education_requirements=education_requirements,
        job_employment_type=emp_type,
        job_seniority_level=seniority,
        job_industries=industries,
        industries=industries,
        job_base_pay_range=base_pay,
        apply_link=apply_link,
        job_summary=job_summary_text,
        certifications=certifications,
        country_code=country_code,
        source_business_url=source_business_url,
        requisition_id=requisition_id,
        metadata=metadata,
        data_quality_warnings=dq_notes,
    )



_BAD_JOB_URL_MARKERS = (
    "savejob",
    "login",
    "signin",
    "sign-in",
    "register",
    "registration",
    "talentcommunity",
    "privacy",
    "cookie",
    "terms",
)

_BAD_JOB_CONTENT_MARKERS = (
    "save application registration",
    "do you have an account? log in",
    "password confirmation",
    "your password must:",
    "automated source picker",
    "first name * last name",
    "create account",
    "sign in to apply",
)

_COMPANY_ONLY_TITLES = {"bloomberg", "careers", "jobs", "job search", "search jobs"}
_MIN_JOB_CONTENT_SCORE = 4
_JOB_DETAIL_CONTENT_MARKERS = (
    "job detail",
    "job description",
    "responsibilities",
    "qualifications",
    "requirements",
    "requisition",
    "apply now",
    "employmenttype",
    "dateposted",
    "validthrough",
)


def clean_description_text(value: str | None) -> str | None:
    """Return readable description text without embedded tags or UI markup noise."""
    if value is None:
        return None
    value = html_lib.unescape(value)
    if "<" in value and ">" in value:
        soup = BeautifulSoup(value, "html.parser")
        for tag in soup.select("script, style, noscript, template, svg, form"):
            tag.decompose()
        value = soup.get_text(" ", strip=True)
    value = re.sub(r"\bskip to (?:main )?content\b", " ", value, flags=re.I)
    value = re.sub(r"\bjavascript is disabled\b", " ", value, flags=re.I)
    return _clean_text(value)


def score_job_listing_content(html: str, url: str) -> JobContentScore:
    """Score whether a fetched page is useful job-listing content.

    Positive signals come from job-detail URL shape, extractable title, structured
    JobPosting metadata, requisition IDs, and job-detail vocabulary. Negative
    signals come from account/save/register flows, generic career-shell titles,
    tiny responses, and sparse visible text.
    """
    raw_html = html or ""
    soup = BeautifulSoup(raw_html, "html.parser")
    visible_text = _description_text(soup) or ""
    lowered_text = visible_text.lower()
    lowered_html = raw_html.lower()
    summary = extract_job_summary(raw_html, url)

    score = 0
    reasons: list[str] = []

    bad_url_markers = _bad_job_url_marker_hits(url)
    if bad_url_markers:
        penalty = 5 + min(len(bad_url_markers) - 1, 2)
        score -= penalty
        reasons.append(f"non-job URL marker: {', '.join(bad_url_markers)}")
    if _is_job_detail_url(url):
        score += 3
        reasons.append("job-detail URL shape")

    if not raw_html or len(raw_html.strip()) < 500:
        score -= 3
        reasons.append("response too small")
    elif len(visible_text) >= 700:
        score += 2
        reasons.append("substantial visible text")
    elif len(visible_text) < 250:
        score -= 2
        reasons.append("very little visible text")

    title = (summary.title or "").strip()
    if title:
        score += 2
        reasons.append("title extracted")
        if title.lower() in _COMPANY_ONLY_TITLES:
            score -= 5
            reasons.append(f"generic site title: {title!r}")
    else:
        score -= 3
        reasons.append("no title extracted")

    if summary.metadata:
        score += 4
        reasons.append("JobPosting metadata")
    if summary.requisition_id:
        score += 1
        reasons.append("requisition id")

    detail_hits = [
        marker for marker in _JOB_DETAIL_CONTENT_MARKERS if marker in lowered_text or marker in lowered_html
    ]
    if detail_hits:
        score += min(len(detail_hits), 5)
        reasons.append(f"job-detail markers: {', '.join(detail_hits[:3])}")

    bad_content_hits = [marker for marker in _BAD_JOB_CONTENT_MARKERS if marker in lowered_text]
    if bad_content_hits:
        score -= 4 + min(len(bad_content_hits), 3)
        reasons.append(f"registration/account markers: {', '.join(bad_content_hits[:3])}")

    return JobContentScore(score=score, reasons=tuple(reasons))


def is_bogus_job_summary(job: JobSummary) -> bool:
    if job.needs_manual_review:
        return False
    score = 0
    title = (job.title or "").strip()
    preview = clean_description_text(job.description or job.description_preview)
    lowered_preview = (preview or "").lower()

    if is_bogus_job_url(job.url):
        score -= 5
    if _is_job_detail_url(job.url):
        score += 2
    if title and title.lower() not in _COMPANY_ONLY_TITLES:
        score += 3
    else:
        score -= 4 if title else 2
    if job.metadata:
        score += 2
    if job.requisition_id:
        score += 1
    if preview and len(preview) >= 120:
        score += 1
    if any(marker in lowered_preview for marker in _JOB_DETAIL_CONTENT_MARKERS):
        score += 2
    if any(marker in lowered_preview for marker in _BAD_JOB_CONTENT_MARKERS):
        score -= 5

    return score < 1


def is_bogus_job_url(url: str) -> bool:
    return bool(_bad_job_url_marker_hits(url))


def validate_job_page(html: str, url: str) -> tuple[bool, str]:
    """Return whether fetched HTML is a usable individual job detail page.

    This intentionally rejects Avature registration/save pages and vague career
    shells that contain job-ish words but not a real job posting.
    """
    content_score = score_job_listing_content(html, url)
    if content_score.score < _MIN_JOB_CONTENT_SCORE:
        reasons = "; ".join(content_score.reasons[:4]) or "no recognizable job-listing signals"
        return False, f"job content score {content_score.score} below {_MIN_JOB_CONTENT_SCORE}: {reasons}."

    return True, f"usable job detail page (score {content_score.score})."


def _looks_like_job_url(url: str, label: str) -> bool:
    parsed = urlparse(url)
    haystack = f"{parsed.path} {parsed.query} {label}"
    lowered = haystack.lower()
    if is_bogus_job_url(url) or any(bad in label.lower() for bad in _BAD_JOB_URL_MARKERS):
        return False
    if any(token in lowered for token in ("jobdetail", "job-detail", "folderdetail", "jobs/detail")):
        return True
    if not _JOB_PATH_RE.search(haystack):
        return False
    return any(token in lowered for token in ("req", "requisition", "position", "opening", "jobid="))


def is_linkedin_hosted_careers_landing(html: str, base_url: str) -> bool:
    """True when the landing page mainly points at LinkedIn job URLs, not native Avature listings (e.g. Xerox)."""
    if not html or "linkedin.com" not in html.lower():
        return False
    soup = BeautifulSoup(html, "html.parser")
    linkedin_job_links = 0
    native_job_links = 0
    for anchor in soup.find_all("a", href=True):
        href_raw = str(anchor.get("href", "")).strip()
        if not href_raw:
            continue
        hlow = href_raw.lower()
        if "linkedin.com/jobs" in hlow or "linkedin.com/job/" in hlow:
            linkedin_job_links += 1
            continue
        try:
            absolute = normalize_url(href_raw, base_url=base_url)
        except ValueError:
            continue
        if not same_host(absolute, base_url):
            continue
        path = urlparse(absolute).path.lower()
        if any(seg in path for seg in ("jobdetail", "job-detail", "folderdetail", "/jobs/detail")):
            native_job_links += 1
    if native_job_links >= 2:
        return False
    if linkedin_job_links >= 2 and native_job_links == 0:
        return True
    if linkedin_job_links >= 5 and native_job_links <= 1:
        return True
    return False


def _selector_text(soup: BeautifulSoup, selector: str) -> str | None:
    found = soup.select_one(selector)
    return _clean_text(found.get_text(" ", strip=True)) if found else None


def _meta_content(soup: BeautifulSoup, property_name: str) -> str | None:
    tag = soup.find("meta", attrs={"property": property_name}) or soup.find(
        "meta", attrs={"name": property_name}
    )
    if not tag:
        return None
    return _clean_text(str(tag.get("content", "")))


def _find_labeled_value(soup: BeautifulSoup, labels: Iterable[str]) -> str | None:
    lowered_labels = tuple(label.lower() for label in labels)
    for element in soup.find_all(string=True):
        text = _clean_text(str(element))
        if not text:
            continue
        lower = text.lower().rstrip(":")
        if lower in lowered_labels:
            parent = element.parent
            if parent and parent.parent:
                candidate = _clean_text(parent.parent.get_text(" ", strip=True))
                for label in lowered_labels:
                    candidate = re.sub(rf"^{re.escape(label)}:?\s*", "", candidate, flags=re.I)
                if candidate and candidate.lower() not in lowered_labels:
                    return candidate
    return None


def _description_text(soup: BeautifulSoup) -> str | None:
    cleaned = BeautifulSoup(str(soup.body or soup), "html.parser")
    for tag in cleaned.select("script, style, noscript, template, svg, form, nav, header, footer"):
        tag.decompose()
    return clean_description_text(cleaned.get_text(" ", strip=True))


def _bad_job_url_marker_hits(url: str) -> list[str]:
    parsed = urlparse(url)
    haystack = f"{parsed.path} {parsed.query}".lower()
    return [marker for marker in _BAD_JOB_URL_MARKERS if marker in haystack]


def _is_job_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    haystack = f"{parsed.path} {parsed.query}".lower()
    return any(token in haystack for token in ("jobdetail", "job-detail", "folderdetail", "jobs/detail"))


def _structured_job_metadata(soup: BeautifulSoup) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "JobPosting":
                for key in ("title", "datePosted", "employmentType", "validThrough"):
                    value = item.get(key)
                    if isinstance(value, str):
                        metadata[key] = value
    return metadata


def _as_clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _organization_name(org: Any) -> str | None:
    if isinstance(org, str):
        return _clean_text(org)
    if isinstance(org, dict):
        return _first_non_empty(_as_clean_str(org.get("name")), _as_clean_str(org.get("legalName")))
    if isinstance(org, list) and org:
        return _organization_name(org[0])
    return None


def _place_to_location_text(place: Any) -> str | None:
    if isinstance(place, str):
        return _clean_text(place)
    if not isinstance(place, dict):
        return None
    addr = place.get("address")
    if isinstance(addr, dict):
        parts = [
            addr.get("streetAddress"),
            addr.get("addressLocality"),
            addr.get("addressRegion"),
            addr.get("postalCode"),
            addr.get("addressCountry"),
        ]
        line = ", ".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())
        return _clean_text(line) if line else None
    return _as_clean_str(place.get("name"))


def _salary_range_string(salary: Any) -> str | None:
    if not isinstance(salary, dict):
        return None
    if salary.get("@type") == "MonetaryAmount":
        val = salary.get("value")
        if isinstance(val, dict) and val.get("@type") == "QuantitativeValue":
            mn = _as_clean_str(val.get("minValue"))
            mx = _as_clean_str(val.get("maxValue"))
            unit = _as_clean_str(val.get("unitText"))
            if mn and mx and mn != mx:
                return f"{mn} - {mx}" + (f" {unit}" if unit else "")
            return _first_non_empty(mn, mx) + (f" {unit}" if unit else "")
    if salary.get("@type") == "QuantitativeValue":
        mn = _as_clean_str(salary.get("minValue"))
        mx = _as_clean_str(salary.get("maxValue"))
        unit = _as_clean_str(salary.get("unitText"))
        if mn and mx:
            return f"{mn} - {mx}" + (f" {unit}" if unit else "")
        return _first_non_empty(mn, mx) + (f" {unit}" if unit else "")
    return None


def _skills_list_to_text(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                s = _clean_text(item)
                if s:
                    parts.append(s)
            elif isinstance(item, dict):
                s = _as_clean_str(item.get("name")) or _as_clean_str(item.get("skill"))
                if s:
                    parts.append(s)
        return ", ".join(parts) if parts else None
    return None


def _iter_ld_jobpostings(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "JobPosting":
                yield item
            graph = item.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if isinstance(node, dict) and node.get("@type") == "JobPosting":
                        yield node


def _jobposting_flat_from_page(soup: BeautifulSoup, page_url: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for item in _iter_ld_jobpostings(soup):
        out["job_posted_date"] = out.get("job_posted_date") or _as_clean_str(item.get("datePosted"))
        out["job_employment_type"] = out.get("job_employment_type") or _as_clean_str(item.get("employmentType"))
        out["apply_link"] = out.get("apply_link") or _as_clean_str(item.get("url"))
        ho = item.get("hiringOrganization")
        out["company_name"] = out.get("company_name") or _organization_name(ho)
        jl = item.get("jobLocation")
        if isinstance(jl, list) and jl:
            jl = jl[0]
        loc_text = _place_to_location_text(jl)
        if loc_text:
            out["job_location_ld"] = out.get("job_location_ld") or loc_text
        if isinstance(jl, dict):
            addr = jl.get("address")
            if isinstance(addr, dict):
                cc = addr.get("addressCountry")
                if isinstance(cc, str) and cc.strip():
                    out["country_code"] = out.get("country_code") or cc.strip().upper()[:2]
        ind = item.get("industry")
        if isinstance(ind, str):
            out["job_industries"] = out.get("job_industries") or _clean_text(ind)
        elif isinstance(ind, list):
            joined = ", ".join(str(x) for x in ind if str(x).strip())
            if joined:
                out["job_industries"] = out.get("job_industries") or _clean_text(joined)
        desc_ld = _as_clean_str(item.get("description"))
        out["job_summary"] = out.get("job_summary") or (desc_ld[:2000] if desc_ld else None)
        out["skills"] = out.get("skills") or _skills_list_to_text(item.get("skills"))
        edu = item.get("educationRequirements")
        if isinstance(edu, str):
            out["education_requirements"] = out.get("education_requirements") or _clean_text(edu)
        elif isinstance(edu, list):
            parts = [_as_clean_str(e) for e in edu]
            joined = "; ".join(p for p in parts if p)
            if joined:
                out["education_requirements"] = out.get("education_requirements") or joined
        out["job_base_pay_range"] = out.get("job_base_pay_range") or _salary_range_string(item.get("baseSalary"))
        occ = item.get("occupationalCategory") or item.get("experienceRequirements")
        out["job_seniority_level"] = out.get("job_seniority_level") or _as_clean_str(
            occ if isinstance(occ, str) else None
        )
        if out.get("apply_link") is None:
            da = item.get("directApply")
            if isinstance(da, str) and da.lower().startswith("http"):
                out["apply_link"] = da
    return out


def _job_location_from_ld(soup: BeautifulSoup) -> str | None:
    flat = _jobposting_flat_from_page(soup, "")
    return flat.get("job_location_ld")


def _company_from_title_brand(title: str | None) -> str | None:
    if not title:
        return None
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        if len(parts) == 2 and len(parts[1].strip()) <= 80:
            return _clean_text(parts[1].strip())
    return None


def _skills_from_ld_list(soup: BeautifulSoup) -> str | None:
    for item in _iter_ld_jobpostings(soup):
        t = _skills_list_to_text(item.get("skills"))
        if t:
            return t
    return None


def _infer_skills_from_text(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    found: list[str] = []
    for kw in _SKILL_KEYWORDS:
        if kw in lower:
            found.append(kw.title() if kw.islower() else kw)
    return ", ".join(dict.fromkeys(found)) if found else None


def _education_snippets_from_text(text: str) -> str | None:
    if not text:
        return None
    matches = _EDUCATION_RE.findall(text)
    if not matches:
        return None
    return "; ".join(dict.fromkeys(m.strip() for m in matches if m.strip()))


def _certifications_from_text(text: str) -> str | None:
    if not text:
        return None
    matches = _CERTIFICATION_RE.findall(text)
    if not matches:
        return None
    return ", ".join(dict.fromkeys(m.strip() for m in matches if m.strip()))


def _find_apply_href(soup: BeautifulSoup, page_url: str) -> str | None:
    for sel in ("a[href*='apply' i]", "a[href*='Apply' i]", "a.button--apply", "a.avature-button--apply"):
        el = soup.select_one(sel)
        if el and el.get("href"):
            try:
                return normalize_url(str(el["href"]), base_url=page_url)
            except ValueError:
                continue
    return None


def _field_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        return not val.strip()
    return False


def apply_headful_inference(job: JobSummary, html: str, url: str, spec: OutputSpec) -> JobSummary:
    """Second-pass enrichment after headful Playwright; always flags manual review."""
    refreshed = extract_job_record(html, url)
    warnings = list(job.data_quality_warnings)
    merged = job.model_dump()
    filled: list[str] = []

    skip_merge = {"data_quality_warnings", "needs_manual_review", "metadata", "url"}
    for name in JobSummary.model_fields:
        if name in skip_merge:
            continue
        old = getattr(job, name)
        new = getattr(refreshed, name)
        if _field_empty(old) and not _field_empty(new):
            merged[name] = new
            filled.append(name)

    if filled:
        warnings.append(f"Inferred or merged after headful fetch: {', '.join(sorted(filled))}.")
    warnings.append(
        "Row is treated as usable but needs_manual_review: verify all fields (especially inferred ones) before downstream use."
    )
    merged["data_quality_warnings"] = warnings
    merged["needs_manual_review"] = True
    return JobSummary.model_validate(merged)


def _extract_requisition_id(text: str, url: str) -> str | None:
    # Avature job-detail URLs usually end in the numeric requisition/job id.
    # Prefer this over body text so we do not accidentally match words like
    # "Requirements" as "req".
    url_match = _URL_JOB_ID_RE.search(url)
    if url_match:
        return url_match.group(1)

    match = _REQ_LABEL_RE.search(text)
    if match:
        return match.group(1)
    return None


def _clean_title(title: str | None) -> str | None:
    if not title:
        return None
    title = html_lib.unescape(title)
    title = re.sub(r"\s+[-|]\s+\d{3,}\s+[-|]\s+[^|]+$", "", title).strip()
    title = re.sub(r"\s+[|]\s+.*$", "", title).strip()
    title = html_lib.unescape(title).strip()
    return title or None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WHITESPACE_RE.sub(" ", html_lib.unescape(value)).strip()
    return cleaned or None


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None
