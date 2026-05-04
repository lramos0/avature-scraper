"""Resolve a stable cache/leaderboard key for the local operator (GitHub-style username preferred)."""

from __future__ import annotations

import getpass
import re
import socket
import subprocess
from urllib.parse import urlparse


def _run_git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=6,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:64] or ""


def _github_user_from_remote(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    m = re.match(r"git@github\.com:([^/]+)/", u, re.I)
    if m:
        return _slug(m.group(1))
    try:
        parsed = urlparse(u)
        host = (parsed.hostname or "").lower()
        if host in {"github.com", "www.github.com"}:
            parts = [p for p in (parsed.path or "").split("/") if p]
            if len(parts) >= 1:
                return _slug(parts[0])
    except ValueError:
        pass
    return ""


def resolve_cache_user_key() -> str:
    """Best-effort: GitHub username > git user.name slug > OS login > hostname."""
    gh = _run_git("config", "--get", "github.user")
    if gh:
        s = _slug(gh)
        if s:
            return s

    for remote in ("origin", "upstream"):
        url = _run_git("config", "--get", f"remote.{remote}.url")
        u = _github_user_from_remote(url)
        if u:
            return u

    name = _run_git("config", "--get", "user.name")
    if name:
        s = _slug(name)
        if s:
            return s

    login = _slug(getpass.getuser())
    if login:
        return login

    return _slug(socket.gethostname()) or "unknown"
