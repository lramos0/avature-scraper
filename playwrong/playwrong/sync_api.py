from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .cdp import (
    CDPEndpoint,
    CDPSession,
    PlaywrongError,
    close_page_target,
    create_page_target,
    detect_open_cdp_browser,
    launch_new_cdp_browser,
)
from .core import PlaywrongSettings, load_settings, log


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    ok: bool = True


class Page:
    def __init__(
        self,
        *,
        settings: PlaywrongSettings,
        base_url: str,
        target_id: str,
        session: CDPSession,
        checkpoint_human: bool,
    ) -> None:
        self._settings = settings
        self._base_url = base_url
        self._target_id = target_id
        self._session = session
        self._checkpoint_human = checkpoint_human
        self._default_timeout_seconds = 30.0
        self._domains_enabled = False

    def _ensure_enabled(self) -> None:
        if self._domains_enabled:
            return
        self._session.send("Page.enable")
        self._session.send("Runtime.enable")
        self._domains_enabled = True

    def set_default_timeout(self, timeout_ms: int) -> None:
        self._default_timeout_seconds = max(1, timeout_ms) / 1000.0

    def set_default_navigation_timeout(self, timeout_ms: int) -> None:
        self.set_default_timeout(timeout_ms)

    def checkpoint(self, message: str = "Checkpoint: press Enter to continue") -> None:
        if not self._checkpoint_human:
            return
        print(f"[playwrong] {message}")
        input()

    def confirm(self, message: str = "scrape page?") -> bool:
        self._ensure_enabled()
        expression = f"window.confirm({json.dumps(message)})"
        result = self._session.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout_seconds=self._default_timeout_seconds,
        )
        value = (((result or {}).get("result") or {}).get("value")) if isinstance(result, dict) else False
        return bool(value)

    def goto(self, url: str, *, wait_until: str = "load", timeout: int | None = None) -> Response:
        self.checkpoint(f"Human checkpoint before navigation to: {url} (press Enter to continue)")
        self._ensure_enabled()
        timeout_seconds = self._default_timeout_seconds if timeout is None else max(1, timeout) / 1000.0
        self._session.send("Page.navigate", {"url": url}, timeout_seconds=timeout_seconds)
        self._wait_ready(wait_until=wait_until, timeout_seconds=timeout_seconds)
        return Response(status=200, url=url, ok=True)

    def _wait_ready(self, *, wait_until: str, timeout_seconds: float) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            result = self._session.send(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
                timeout_seconds=min(5.0, timeout_seconds),
            )
            value = (
                (((result or {}).get("result") or {}).get("value"))
                if isinstance(result, dict)
                else None
            )
            state = str(value or "")
            if wait_until == "domcontentloaded":
                if state in {"interactive", "complete"}:
                    return
            else:
                if state == "complete":
                    return
            time.sleep(0.1)
        raise PlaywrongError(f"Timed out waiting for page load state={wait_until!r}")

    def content(self) -> str:
        self._ensure_enabled()
        result = self._session.send(
            "Runtime.evaluate",
            {
                "expression": "document.documentElement ? document.documentElement.outerHTML : ''",
                "returnByValue": True,
            },
        )
        value = (((result or {}).get("result") or {}).get("value")) if isinstance(result, dict) else ""
        return str(value or "")

    def close(self) -> None:
        self._session.close()
        close_page_target(self._base_url, self._target_id)


class FirefoxPage:
    def __init__(self, *, driver: Any, checkpoint_human: bool) -> None:
        self._driver = driver
        self._checkpoint_human = checkpoint_human

    def set_default_timeout(self, timeout_ms: int) -> None:
        _ = timeout_ms
        return

    def set_default_navigation_timeout(self, timeout_ms: int) -> None:
        _ = timeout_ms
        return

    def checkpoint(self, message: str = "Checkpoint: press Enter to continue") -> None:
        if not self._checkpoint_human:
            return
        print(f"[playwrong] {message}")
        input()

    def confirm(self, message: str = "scrape page?") -> bool:
        try:
            # Blocks on a native browser confirm dialog until the human responds.
            value = self._driver.execute_script("return window.confirm(arguments[0]);", message)
            return bool(value)
        except Exception:
            return False

    def goto(self, url: str, *, wait_until: str = "load", timeout: int | None = None) -> Response:
        _ = wait_until, timeout
        self.checkpoint(f"Human checkpoint before navigation to: {url} (press Enter to continue)")
        self._driver.get(url)
        current_url = str(getattr(self._driver, "current_url", url) or url)
        return Response(status=200, url=current_url, ok=True)

    def content(self) -> str:
        value = getattr(self._driver, "page_source", "")
        return str(value or "")

    def close(self) -> None:
        return


class BrowserContext:
    def __init__(self, browser: "Browser") -> None:
        self._browser = browser

    def new_page(self) -> Page:
        return self._browser.new_page()

    def close(self) -> None:
        return


class FirefoxBrowserContext:
    def __init__(self, browser: "FirefoxBrowser") -> None:
        self._browser = browser

    def new_page(self) -> FirefoxPage:
        return self._browser.new_page()

    def close(self) -> None:
        return


class Browser:
    def __init__(
        self,
        *,
        settings: PlaywrongSettings,
        endpoint: CDPEndpoint,
        checkpoint_human: bool,
        launched_process: Any | None,
    ) -> None:
        self._settings = settings
        self._endpoint = endpoint
        self._checkpoint_human = checkpoint_human
        self._launched_process = launched_process
        self._pages: list[Page] = []
        self._default_context = BrowserContext(self)

    @property
    def contexts(self) -> list[BrowserContext]:
        return [self._default_context]

    def new_context(self, **_: Any) -> BrowserContext:
        return self._default_context

    def new_page(self) -> Page:
        target = create_page_target(self._endpoint.base_url, "about:blank")
        target_id = str(target.get("id", "")).strip()
        ws_url = str(target.get("webSocketDebuggerUrl", "")).strip()
        if not target_id or not ws_url:
            raise PlaywrongError("Failed to create CDP page target.")
        session = CDPSession(ws_url)
        page = Page(
            settings=self._settings,
            base_url=self._endpoint.base_url,
            target_id=target_id,
            session=session,
            checkpoint_human=self._checkpoint_human,
        )
        self._pages.append(page)
        return page

    def close(self) -> None:
        for page in list(self._pages):
            try:
                page.close()
            except Exception:
                pass
        self._pages.clear()
        if self._launched_process is not None:
            self._launched_process.terminate()


class FirefoxBrowser:
    def __init__(
        self,
        *,
        settings: PlaywrongSettings,
        driver: Any,
        checkpoint_human: bool,
    ) -> None:
        self._settings = settings
        self._driver = driver
        self._checkpoint_human = checkpoint_human
        self._pages: list[FirefoxPage] = []
        self._default_context = FirefoxBrowserContext(self)

    @property
    def contexts(self) -> list[FirefoxBrowserContext]:
        return [self._default_context]

    def new_context(self, **_: Any) -> FirefoxBrowserContext:
        return self._default_context

    def new_page(self) -> FirefoxPage:
        page = FirefoxPage(driver=self._driver, checkpoint_human=self._checkpoint_human)
        self._pages.append(page)
        return page

    def close(self) -> None:
        try:
            self._driver.quit()
        except Exception:
            pass
        self._pages.clear()


class BrowserType:
    def __init__(self, *, name: str, settings: PlaywrongSettings) -> None:
        self._name = name
        self._settings = settings

    def _unsupported(self) -> None:
        raise PlaywrongError(
            f"{self._name} is not implemented in playwrong custom driver. "
            "Supported browser types are chromium and firefox."
        )

    def launch(
        self,
        *,
        headless: bool = False,
        prefer_open: bool | None = None,
        checkpoint_human: bool = False,
        browser_path: str | None = None,
        debug_port: int = 9222,
        **kwargs: Any,
    ) -> Browser | FirefoxBrowser:
        if self._name == "chromium":
            should_prefer_open = self._settings.prefer_open if prefer_open is None else prefer_open
            open_endpoint = detect_open_cdp_browser(self._settings) if should_prefer_open else None
            if open_endpoint is not None:
                log(self._settings, f"Attaching to open browser: {open_endpoint.base_url}")
                return Browser(
                    settings=self._settings,
                    endpoint=open_endpoint,
                    checkpoint_human=checkpoint_human,
                    launched_process=None,
                )

            process, endpoint = launch_new_cdp_browser(
                self._settings,
                headless=headless,
                browser_path=browser_path,
                debug_port=debug_port,
            )
            return Browser(
                settings=self._settings,
                endpoint=endpoint,
                checkpoint_human=checkpoint_human,
                launched_process=process,
            )

        if self._name == "firefox":
            if self._settings.prefer_open if prefer_open is None else prefer_open:
                log(
                    self._settings,
                    "Firefox open-session attach is not supported by undetected-geckodriver; launching a new session.",
                )
            try:
                from selenium.webdriver.firefox.options import Options as FirefoxOptions
            except ImportError as exc:
                raise PlaywrongError(
                    "selenium is required for firefox backend. Install: pip install selenium"
                ) from exc

            try:
                from undetected_geckodriver import Firefox as UndetectedFirefox
            except ImportError as exc:
                raise PlaywrongError(
                    "Firefox backend requires undetected geckodriver.\n"
                    "Install one of:\n"
                    "  pip install undetected-geckodriver selenium\n"
                    "  pip install undetected-geckodriver-lw selenium"
                ) from exc

            options = FirefoxOptions()
            if headless:
                options.add_argument("-headless")
            options.add_argument("-no-remote")
            options.add_argument("-new-instance")
            if browser_path:
                options.binary_location = browser_path

            geckodriver_path = kwargs.get("geckodriver_path")
            marionette_port = kwargs.get("marionette_port")

            try:
                driver = UndetectedFirefox(
                    options=options,
                    geckodriver_path=geckodriver_path,
                    headless=headless,
                    marionette_port=marionette_port,
                )
            except TypeError:
                # Some versions may not accept keyword-only options signatures.
                driver = UndetectedFirefox(options=options)
                if headless:
                    try:
                        driver.set_window_size(1280, 800)
                    except Exception:
                        pass

            return FirefoxBrowser(
                settings=self._settings,
                driver=driver,
                checkpoint_human=checkpoint_human,
            )

        self._unsupported()
        raise PlaywrongError(f"Unsupported browser type: {self._name}")

    def connect_over_cdp(self, endpoint: str, *, checkpoint_human: bool = False, **_: Any) -> Browser:
        if self._name != "chromium":
            self._unsupported()
        parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        if not parsed.scheme or not parsed.netloc:
            raise PlaywrongError(f"Invalid CDP endpoint: {endpoint}")
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        discovered = CDPEndpoint(base_url=base_url, browser_ws_url="", browser_name="manual")
        return Browser(
            settings=self._settings,
            endpoint=discovered,
            checkpoint_human=checkpoint_human,
            launched_process=None,
        )


class _Playwrong:
    def __init__(self, settings: PlaywrongSettings) -> None:
        self._settings = settings
        self.chromium = BrowserType(name="chromium", settings=settings)
        self.firefox = BrowserType(name="firefox", settings=settings)
        self.webkit = BrowserType(name="webkit", settings=settings)

    def stop(self) -> None:
        return


class _PlaywrongContextManager:
    def __init__(
        self,
        *,
        prefer_open: bool | None = None,
        cdp_endpoints: list[str] | None = None,
        cdp_timeout_seconds: float | None = None,
        verbose: bool | None = None,
    ) -> None:
        self._settings = load_settings(
            prefer_open=prefer_open,
            cdp_endpoints=cdp_endpoints,
            connect_timeout_seconds=cdp_timeout_seconds,
            verbose=verbose,
        )
        self._instance: _Playwrong | None = None

    def __enter__(self) -> _Playwrong:
        self._instance = _Playwrong(self._settings)
        return self._instance

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._instance is not None:
            self._instance.stop()
            self._instance = None

    def start(self) -> _Playwrong:
        return self.__enter__()

    def stop(self) -> None:
        self.__exit__(None, None, None)


def sync_playwright(
    *,
    prefer_open: bool | None = None,
    cdp_endpoints: list[str] | None = None,
    cdp_timeout_seconds: float | None = None,
    verbose: bool | None = None,
) -> _PlaywrongContextManager:
    return _PlaywrongContextManager(
        prefer_open=prefer_open,
        cdp_endpoints=cdp_endpoints,
        cdp_timeout_seconds=cdp_timeout_seconds,
        verbose=verbose,
    )


__all__ = [
    "PlaywrongError",
    "sync_playwright",
]
