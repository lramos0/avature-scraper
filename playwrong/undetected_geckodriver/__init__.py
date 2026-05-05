from __future__ import annotations

import socket
from pathlib import Path
from shutil import which
from typing import Any

from selenium import webdriver
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService


def _first_existing(paths: list[str]) -> str | None:
    for path in paths:
        if not path:
            continue
        if Path(path).exists():
            return path
    return None


def _is_probably_native_binary(path: str) -> bool:
    try:
        candidate = Path(path)
        if not candidate.exists() or not candidate.is_file():
            return False
        with candidate.open("rb") as handle:
            magic = handle.read(4)
        return magic.startswith(b"\x7fELF") or magic.startswith(b"MZ")
    except OSError:
        return False


def _snap_direct_binary_from_wrapper(path: str) -> str | None:
    lowered = path.replace("\\", "/").lower()
    if lowered.endswith("/usr/bin/firefox") or lowered.endswith("/snap/bin/firefox"):
        candidate = "/snap/firefox/current/usr/lib/firefox/firefox"
        if _is_probably_native_binary(candidate):
            return candidate
    return None


def _resolve_firefox_binary(explicit: str | None) -> str | None:
    checked: list[str] = []

    def add(path: str | None) -> None:
        if not path:
            return
        if path not in checked:
            checked.append(path)

    if explicit:
        expanded = str(Path(explicit).expanduser())
        add(expanded)
        snap_direct = _snap_direct_binary_from_wrapper(expanded)
        if snap_direct:
            add(snap_direct)

    add(which("firefox"))
    add(which("firefox-esr"))
    common = [
        "/snap/firefox/current/usr/lib/firefox/firefox",
        "/usr/bin/firefox",
        "/usr/bin/firefox-esr",
        "/snap/bin/firefox",
        "/usr/lib/firefox/firefox",
        "C:\\Program Files\\Mozilla Firefox\\firefox.exe",
        "C:\\Program Files (x86)\\Mozilla Firefox\\firefox.exe",
    ]
    for item in common:
        add(item)

    for path in checked:
        if _is_probably_native_binary(path):
            return path

    return None


def _find_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Firefox(webdriver.Firefox):
    """Local undetected-geckodriver-style wrapper with Windows/Linux support."""

    def __init__(
        self,
        *,
        options: FirefoxOptions | None = None,
        firefox_binary: str | None = None,
        geckodriver_path: str | None = None,
        service: FirefoxService | None = None,
        headless: bool = False,
        marionette_port: int | None = None,
        **kwargs: Any,
    ) -> None:
        resolved_options = options or FirefoxOptions()
        if headless:
            resolved_options.add_argument("-headless")

        resolved_binary = _resolve_firefox_binary(firefox_binary)
        if resolved_binary:
            resolved_options.binary_location = resolved_binary

        resolved_service = service
        if resolved_service is None:
            requested_marionette_port = marionette_port if marionette_port and marionette_port > 0 else _find_free_tcp_port()
            service_args = ["--marionette-port", str(requested_marionette_port)]
            if geckodriver_path:
                resolved_service = FirefoxService(executable_path=geckodriver_path, service_args=service_args)
            else:
                resolved_service = FirefoxService(service_args=service_args)

        try:
            super().__init__(options=resolved_options, service=resolved_service, **kwargs)
        except Exception as exc:
            message = str(exc)
            if "binary is not a Firefox executable" in message:
                raise RuntimeError(
                    "Could not launch Firefox: detected path is not a native Firefox binary.\n"
                    "Try setting one of these explicitly:\n"
                    "  --browser-executable-path /snap/firefox/current/usr/lib/firefox/firefox\n"
                    "  --browser-executable-path /usr/lib/firefox/firefox\n"
                    "Also ensure geckodriver is installed and available."
                ) from exc
            raise

        try:
            self.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:
            pass


__all__ = ["Firefox"]
