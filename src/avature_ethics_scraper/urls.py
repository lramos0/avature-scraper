"""URL validation and normalization utilities."""

from __future__ import annotations

import hashlib
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse


class UrlValidationError(ValueError):
    """Raised when a URL should not be fetched."""


# Common Avature / career-site entry paths (try in order after any explicit seed path).
_CAREER_LANDING_REL_PATHS: tuple[str, ...] = ("/careers", "/eng_US/careers", "/jobs")


def career_landing_url_candidates(seed: str) -> list[str]:
    """Return ordered absolute URLs to try for a career homepage (same origin as *seed*).

    Always includes ``/careers``, ``/eng_US/careers``, and ``/jobs``. If *seed* has a non-root
    path, that URL is tried first (once).
    """
    normalized = normalize_url(seed)
    parsed = urlparse(normalized)
    origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
    seen: set[str] = set()
    ordered: list[str] = []

    def push(url: str) -> None:
        u = normalize_url(url)
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    path = (parsed.path or "").rstrip("/")
    if path:
        push(normalized)
    for rel in _CAREER_LANDING_REL_PATHS:
        push(normalize_url(rel, base_url=origin))
    return ordered


def normalize_url(url: str, *, base_url: str | None = None) -> str:
    """Return an absolute, defragmented HTTP(S) URL."""
    candidate = urljoin(base_url, url) if base_url else url
    candidate, _fragment = urldefrag(candidate.strip())
    parsed = urlparse(candidate)

    if parsed.scheme not in {"http", "https"}:
        raise UrlValidationError(f"Only HTTP(S) URLs are supported: {candidate!r}")
    if not parsed.netloc:
        raise UrlValidationError(f"URL is missing a host: {candidate!r}")

    normalized_path = parsed.path or "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), normalized_path, "", parsed.query, ""))


def robots_url_for(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    return urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))


def same_host(left: str, right: str) -> bool:
    return urlparse(left).netloc.lower() == urlparse(right).netloc.lower()


def stable_id_from_url(url: str) -> str:
    """Deterministic stable id when no requisition id exists (sha256 of normalized URL, hex prefix)."""
    normalized = normalize_url(url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"url-{digest[:24]}"
