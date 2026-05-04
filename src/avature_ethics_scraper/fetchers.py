"""Progressive fetchers: requests, Playwright headless, then Playwright headful."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .models import FetchMethod, FetchResult


@dataclass(frozen=True)
class FetchSettings:
    user_agent: str
    timeout_seconds: float = 10.0
    initial_read_timeout_seconds: float = 5.0

    @property
    def extended_timeout_seconds(self) -> float:
        return max(self.timeout_seconds, self.initial_read_timeout_seconds)


@dataclass(frozen=True)
class ContentProbe:
    state: str  # "usable", "bogus", or "inconclusive"
    reason: str


class RequestsFetcher:
    def __init__(self, settings: FetchSettings) -> None:
        self._settings = settings

    def fetch(self, url: str, *, require_job_page: bool = False) -> FetchResult:
        started_at = time.monotonic()
        try:
            response = requests.get(
                url,
                stream=True,
                timeout=(
                    self._settings.initial_read_timeout_seconds,
                    self._settings.extended_timeout_seconds,
                ),
                headers={
                    "User-Agent": self._settings.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        except requests.RequestException as exc:
            return FetchResult(
                url=url,
                method=FetchMethod.REQUESTS,
                ok=False,
                error=f"{exc.__class__.__name__}: {exc}",
            )

        content_type = response.headers.get("content-type")
        chunks: list[bytes] = []
        probe_after = self._settings.initial_read_timeout_seconds
        deadline = started_at + self._settings.extended_timeout_seconds
        early_probe_done = False

        try:
            for chunk in response.iter_content(chunk_size=16384):
                if chunk:
                    chunks.append(chunk)

                elapsed = time.monotonic() - started_at
                should_probe = not early_probe_done and elapsed >= probe_after
                enough_to_probe = sum(len(part) for part in chunks) >= 4096
                if not early_probe_done and (should_probe or enough_to_probe):
                    early_probe_done = True
                    preview = _decode_chunks(chunks, response.encoding)
                    probe = _probe_content(preview, url, require_job_page=require_job_page, partial=True)
                    if probe.state == "bogus":
                        response.close()
                        return FetchResult(
                            url=url,
                            method=FetchMethod.REQUESTS,
                            ok=False,
                            status_code=response.status_code,
                            content=preview,
                            content_type=content_type,
                            error=f"garbage/non-job data detected early: {probe.reason}",
                        )

                if time.monotonic() > deadline:
                    break
        except requests.RequestException as exc:
            preview = _decode_chunks(chunks, response.encoding)
            probe = _probe_content(preview, url, require_job_page=require_job_page, partial=True)
            if probe.state == "bogus":
                return FetchResult(
                    url=url,
                    method=FetchMethod.REQUESTS,
                    ok=False,
                    status_code=response.status_code,
                    content=preview,
                    content_type=content_type,
                    error=f"garbage/non-job data detected early: {probe.reason}",
                )
            return FetchResult(
                url=url,
                method=FetchMethod.REQUESTS,
                ok=False,
                status_code=response.status_code,
                content=preview,
                content_type=content_type,
                error=f"{exc.__class__.__name__}: {exc}",
            )

        text = _decode_chunks(chunks, response.encoding)
        probe = _probe_content(text, url, require_job_page=require_job_page, partial=False)
        ok = response.ok and probe.state == "usable"
        return FetchResult(
            url=url,
            method=FetchMethod.REQUESTS,
            ok=ok,
            status_code=response.status_code,
            content=text,
            content_type=content_type,
            error=None if ok else (None if not response.ok else probe.reason),
        )


class PlaywrightFetcher:
    def __init__(self, settings: FetchSettings, *, headless: bool) -> None:
        self._settings = settings
        self._headless = headless

    def fetch(self, url: str, *, require_job_page: bool = False) -> FetchResult:
        method = FetchMethod.PLAYWRIGHT_HEADLESS if self._headless else FetchMethod.PLAYWRIGHT_HEADFUL
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(
                url=url,
                method=method,
                ok=False,
                error="Playwright is not installed. Install with: pip install 'aventure-scraper[browser]' && python -m playwright install chromium",
            )

        content = ""
        status_code: int | None = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self._headless)
                context = browser.new_context(user_agent=self._settings.user_agent)
                page = context.new_page()

                try:
                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self._settings.initial_read_timeout_seconds * 1000),
                    )
                    status_code = response.status if response else None
                except PlaywrightTimeoutError:
                    response = None

                content = page.content()
                probe = _probe_content(content, url, require_job_page=require_job_page, partial=True)
                if probe.state == "bogus":
                    browser.close()
                    return FetchResult(
                        url=url,
                        method=method,
                        ok=False,
                        status_code=status_code,
                        content=content,
                        error=f"garbage/non-job data detected early: {probe.reason}",
                    )

                if probe.state == "inconclusive":
                    remaining = max(
                        0.0,
                        self._settings.extended_timeout_seconds - self._settings.initial_read_timeout_seconds,
                    )
                    if remaining > 0:
                        try:
                            page.wait_for_load_state("networkidle", timeout=int(remaining * 1000))
                        except PlaywrightTimeoutError:
                            pass
                        content = page.content()

                browser.close()
        except Exception as exc:  # Playwright raises several runtime-specific exception classes.
            return FetchResult(
                url=url,
                method=method,
                ok=False,
                status_code=status_code,
                content=content,
                error=f"{exc.__class__.__name__}: {exc}",
            )

        probe = _probe_content(content, url, require_job_page=require_job_page, partial=False)
        return FetchResult(
            url=url,
            method=method,
            ok=probe.state == "usable",
            status_code=status_code,
            content=content,
            error=None if probe.state == "usable" else probe.reason,
        )


def _decode_chunks(chunks: list[bytes], encoding: str | None) -> str:
    raw = b"".join(chunks)
    return raw.decode(encoding or "utf-8", errors="replace") if raw else ""


def _probe_content(content: str, url: str, *, require_job_page: bool, partial: bool) -> ContentProbe:
    """Classify fetched content without waiting for the full timeout when it is clearly bad."""
    lowered = content.lower()
    hard_bogus_markers = (
        "save application registration",
        "do you have an account? log in",
        "password confirmation",
        "your password must:",
        "automated source picker",
        "first name * last name",
        "create account",
        "sign in to apply",
        "<title>login",
        "access denied",
        "captcha",
    )
    if any(marker in lowered for marker in hard_bogus_markers):
        return ContentProbe("bogus", "page is a login, registration, save-job, CAPTCHA, or access-control flow.")

    if require_job_page:
        from .extract import validate_job_page

        if not content or len(content.strip()) < 500:
            return ContentProbe("inconclusive", "not enough content was available yet to validate a job page.")
        is_job, reason = validate_job_page(content, url)
        if is_job:
            return ContentProbe("usable", reason)
        if partial:
            if _is_obviously_wrong_job_document(content, url, reason):
                return ContentProbe("bogus", reason)
            if _may_become_usable_job_content(content):
                return ContentProbe("inconclusive", reason)
        return ContentProbe("bogus", reason)

    if _looks_like_job_or_listing_html(content):
        return ContentProbe("usable", "recognizable Avature career/job listing content was present.")
    if not content or len(content.strip()) < 300:
        return ContentProbe("inconclusive", "not enough content was available yet.")
    if partial and _may_become_usable_job_content(content):
        return ContentProbe("inconclusive", "content is still loading and may become recognizable.")
    return ContentProbe("bogus", "content did not look like Avature career/job listing data.")


def _is_obviously_wrong_job_document(content: str, url: str, reason: str) -> bool:
    """Return True when another fetch method should be tried immediately.

    Avature pages often include generic career/search chrome early in the HTML. That
    can make a partial document look promising even when the actual payload is a
    search page, login page, or other non-detail page. These cases should not sit
    around waiting for the full timeout.
    """
    lowered = content.lower()
    reason_lower = reason.lower()
    if "company/site title" in reason_lower or "account/save-job" in reason_lower:
        return True
    if "body--search-jobs" in lowered or "section--search-jobs" in lowered:
        return True
    if "list-controls__pagination" in lowered and "article--result" in lowered:
        return True
    return False


def _may_become_usable_job_content(content: str) -> bool:
    lowered = content.lower()
    soft_markers = ("avature", "career", "careers", "job", "jobs", "opening", "position", "requisition")
    return any(marker in lowered for marker in soft_markers)


def _looks_like_job_or_listing_html(content: str) -> bool:
    """Heuristic guard against login pages, empty shells, and unrelated content."""
    if not content or len(content.strip()) < 300:
        return False

    lowered = content.lower()
    negative_markers = ("<title>login", "sign in", "access denied", "captcha")
    if any(marker in lowered for marker in negative_markers):
        return False

    positive_markers = (
        "avature",
        "jobdetail",
        "job detail",
        "apply",
        "requisition",
        "position",
        "career",
        "opening",
        "location",
    )
    return any(marker in lowered for marker in positive_markers)
