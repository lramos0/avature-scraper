from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from websocket import WebSocket, WebSocketTimeoutException, create_connection

from .core import PlaywrongSettings, iter_reachable_endpoints, log


class PlaywrongError(RuntimeError):
    pass


@dataclass(frozen=True)
class CDPEndpoint:
    base_url: str
    browser_ws_url: str
    browser_name: str


def _normalize_base_url(endpoint: str) -> str:
    raw = endpoint.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _fetch_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
        payload = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise PlaywrongError(f"Expected JSON object from {url}, got: {type(parsed).__name__}")
    return parsed


def detect_open_cdp_browser(settings: PlaywrongSettings) -> CDPEndpoint | None:
    for endpoint in iter_reachable_endpoints(settings):
        base_url = _normalize_base_url(endpoint)
        if not base_url:
            continue
        version_url = f"{base_url}/json/version"
        try:
            payload = _fetch_json(version_url, timeout_seconds=settings.connect_timeout_seconds)
        except (PlaywrongError, URLError, TimeoutError, ValueError):
            continue
        ws_url = str(payload.get("webSocketDebuggerUrl", "")).strip()
        if not ws_url:
            continue
        browser_name = str(payload.get("Browser", "")).strip()
        log(settings, f"Detected open CDP browser at {base_url} ({browser_name or 'unknown'}).")
        return CDPEndpoint(base_url=base_url, browser_ws_url=ws_url, browser_name=browser_name)
    return None


def _find_chromium_executable(explicit_path: str | None) -> str:
    candidates: list[str] = []
    if explicit_path:
        candidates.append(str(Path(explicit_path).expanduser()))
    candidates.extend(
        [
            "google-chrome",
            "chromium-browser",
            "chromium",
            "microsoft-edge",
            "msedge",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
            "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
            "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
        ]
    )

    for candidate in candidates:
        if not candidate:
            continue
        if "/" not in candidate and "\\" not in candidate:
            from shutil import which

            resolved = which(candidate)
            if resolved:
                return resolved
            continue
        if Path(candidate).exists():
            return candidate
    raise PlaywrongError(
        "Could not find a Chromium executable. Pass browser_path explicitly or install Chrome/Chromium."
    )


def launch_new_cdp_browser(
    settings: PlaywrongSettings,
    *,
    headless: bool,
    browser_path: str | None,
    debug_port: int,
) -> tuple[subprocess.Popen[bytes], CDPEndpoint]:
    executable = _find_chromium_executable(browser_path)
    user_data_dir = tempfile.mkdtemp(prefix="playwrong-chromium-profile-")

    args = [
        executable,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    if headless:
        args.insert(1, "--headless=new")

    log(settings, f"Launching Chromium: {' '.join(args)}")
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    base_url = f"http://127.0.0.1:{debug_port}"
    deadline = time.time() + 15.0
    last_error = ""
    while time.time() < deadline:
        try:
            payload = _fetch_json(f"{base_url}/json/version", timeout_seconds=0.5)
            ws_url = str(payload.get("webSocketDebuggerUrl", "")).strip()
            if ws_url:
                browser_name = str(payload.get("Browser", "")).strip()
                endpoint = CDPEndpoint(base_url=base_url, browser_ws_url=ws_url, browser_name=browser_name)
                return process, endpoint
        except Exception as exc:  # pragma: no cover - transient startup errors
            last_error = str(exc)
            time.sleep(0.2)
    process.terminate()
    raise PlaywrongError(
        f"Chromium launched but CDP endpoint did not become available at {base_url}. Last error: {last_error}"
    )


class CDPSession:
    def __init__(self, ws_url: str, *, timeout_seconds: float = 30.0) -> None:
        self.ws_url = ws_url
        self.ws: WebSocket = create_connection(ws_url, timeout=timeout_seconds)
        self._next_id = 1
        self._events: list[dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, method: str, params: dict[str, Any] | None = None, *, timeout_seconds: float = 30.0) -> Any:
        payload: dict[str, Any] = {"id": self._next_id, "method": method}
        if params:
            payload["params"] = params
        request_id = self._next_id
        self._next_id += 1
        self.ws.send(json.dumps(payload))

        original_timeout = self.ws.gettimeout()
        self.ws.settimeout(timeout_seconds)
        try:
            while True:
                raw = self.ws.recv()
                message = json.loads(raw)
                if isinstance(message, dict) and message.get("id") == request_id:
                    if "error" in message:
                        raise PlaywrongError(f"CDP error for {method}: {message['error']}")
                    return message.get("result")
                if isinstance(message, dict) and "method" in message:
                    self._events.append(message)
        except WebSocketTimeoutException as exc:
            raise PlaywrongError(f"Timed out waiting for CDP response to {method}") from exc
        finally:
            self.ws.settimeout(original_timeout)

    def pop_event(self, method_name: str) -> dict[str, Any] | None:
        for idx, event in enumerate(self._events):
            if event.get("method") == method_name:
                return self._events.pop(idx)
        return None


def create_page_target(base_url: str, initial_url: str = "about:blank") -> dict[str, Any]:
    encoded = quote(initial_url, safe=":/?&=%#")
    # Edge's remote debugging endpoint expects PUT /json/new and uses a query param `url=...`.
    # If we use GET or omit the `url=` key, Edge returns HTTP 405 Method Not Allowed.
    target_url = f"{base_url}/json/new?url={encoded}"
    req = Request(target_url, method="PUT")
    with urlopen(req, timeout=5.0) as response:  # noqa: S310
        payload = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise PlaywrongError("Unexpected /json/new response type.")
    return parsed


def close_page_target(base_url: str, target_id: str) -> None:
    with urlopen(f"{base_url}/json/close/{target_id}", timeout=5.0):  # noqa: S310
        return
