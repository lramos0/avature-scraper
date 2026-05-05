from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse


DEFAULT_CDP_ENDPOINTS: tuple[str, ...] = (
    "http://127.0.0.1:9222",
    "http://localhost:9222",
    "http://127.0.0.1:9223",
    "http://localhost:9223",
    "http://127.0.0.1:9333",
    "http://localhost:9333",
)

DEFAULT_MARIONETTE_ENDPOINTS: tuple[str, ...] = (
    "127.0.0.1:2828",
    "localhost:2828",
)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _parse_endpoints(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    items = [part.strip() for part in raw.split(",")]
    deduped: list[str] = []
    for item in items:
        if not item:
            continue
        if item not in deduped:
            deduped.append(item)
    return tuple(deduped)


@dataclass(frozen=True)
class PlaywrongSettings:
    prefer_open: bool
    cdp_endpoints: tuple[str, ...]
    marionette_endpoints: tuple[str, ...]
    connect_timeout_seconds: float
    verbose: bool


def load_settings(
    *,
    prefer_open: bool | None = None,
    cdp_endpoints: Iterable[str] | None = None,
    marionette_endpoints: Iterable[str] | None = None,
    connect_timeout_seconds: float | None = None,
    verbose: bool | None = None,
) -> PlaywrongSettings:
    env_cdp_endpoints = _parse_endpoints(os.getenv("PLAYWRONG_CDP_ENDPOINTS"))
    if cdp_endpoints is not None:
        deduped = list(dict.fromkeys(str(v).strip() for v in cdp_endpoints if str(v).strip()))
        resolved_cdp_endpoints = tuple(deduped)
    elif env_cdp_endpoints:
        resolved_cdp_endpoints = env_cdp_endpoints
    else:
        resolved_cdp_endpoints = DEFAULT_CDP_ENDPOINTS

    env_marionette_endpoints = _parse_endpoints(os.getenv("PLAYWRONG_MARIONETTE_ENDPOINTS"))
    if marionette_endpoints is not None:
        deduped = list(dict.fromkeys(str(v).strip() for v in marionette_endpoints if str(v).strip()))
        resolved_marionette_endpoints = tuple(deduped)
    elif env_marionette_endpoints:
        resolved_marionette_endpoints = env_marionette_endpoints
    else:
        resolved_marionette_endpoints = DEFAULT_MARIONETTE_ENDPOINTS

    return PlaywrongSettings(
        prefer_open=_bool_env("PLAYWRONG_PREFER_OPEN", True) if prefer_open is None else prefer_open,
        cdp_endpoints=resolved_cdp_endpoints,
        marionette_endpoints=resolved_marionette_endpoints,
        connect_timeout_seconds=(
            _float_env("PLAYWRONG_CDP_TIMEOUT_SECONDS", 0.2)
            if connect_timeout_seconds is None
            else connect_timeout_seconds
        ),
        verbose=_bool_env("PLAYWRONG_VERBOSE", True) if verbose is None else verbose,
    )


def log(settings: PlaywrongSettings, message: str) -> None:
    if not settings.verbose:
        return
    print(f"[playwrong] {message}", file=sys.stderr, flush=True)


def _normalize_endpoint(endpoint: str) -> tuple[str, int] | None:
    candidate = endpoint.strip()
    if "://" not in candidate:
        candidate = f"tcp://{candidate}"
    parsed = urlparse(candidate)
    host = parsed.hostname
    if not host:
        return None
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme in {"http", "ws"}:
        port = 80
    elif parsed.scheme in {"https", "wss"}:
        port = 443
    else:
        port = 9222
    return host, port


def is_endpoint_reachable(endpoint: str, timeout_seconds: float) -> bool:
    normalized = _normalize_endpoint(endpoint)
    if normalized is None:
        return False
    host, port = normalized
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def iter_reachable_endpoints(settings: PlaywrongSettings) -> Iterable[str]:
    for endpoint in settings.cdp_endpoints:
        if is_endpoint_reachable(endpoint, settings.connect_timeout_seconds):
            yield endpoint
