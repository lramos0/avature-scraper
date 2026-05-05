"""Progressive fetchers: requests, then playwrong browser tiers (headless/headful/CDP).

Headful/headless/CDP automation uses the **playwrong** package (this repo's ``playwrong/`` tree —
CDP over WebSocket, API names mirror Playwright for familiarity). It is **not** ``pip install playwright``.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from typing import Any

from .console import console
from .models import FetchMethod, FetchResult


def wslg_display_env_for_subprocess() -> dict[str, str]:
    """Extra env vars for worker processes when WSLg is present but DISPLAY was not exported."""
    if sys.platform == "win32":
        return {}
    if os.environ.get("DISPLAY", "").strip() or os.environ.get("WAYLAND_DISPLAY", "").strip():
        return {}
    if Path("/mnt/wslg").is_dir():
        return {"DISPLAY": ":0"}
    return {}


def apply_wslg_display_if_needed() -> None:
    """Set DISPLAY in-process for WSLg (non-login shells often omit it). Safe to call at CLI startup."""
    os.environ.update(wslg_display_env_for_subprocess())


def _ensure_display_for_headful_unix() -> str | None:
    """Return an error string if headful GUI cannot run; else None."""
    apply_wslg_display_if_needed()
    if sys.platform == "win32":
        return None
    if os.environ.get("DISPLAY", "").strip() or os.environ.get("WAYLAND_DISPLAY", "").strip():
        return None
    return (
        "Headful browser needs a display, but DISPLAY and WAYLAND_DISPLAY are unset. "
        "Fix: run `export DISPLAY=:0` (WSLg) or configure your X server, in the same environment "
        "that starts parallelize / avature-scraper so child processes inherit it."
    )


def _x_display_socket_ready() -> bool:
    """Best-effort check that an X display socket exists (to avoid hanging Firefox startups)."""
    if sys.platform == "win32":
        return True
    display = (os.environ.get("DISPLAY") or "").strip()
    if not display:
        return False
    if display.startswith(":"):
        # Typical local X display in Linux/WSL.
        suffix = display[1:].split(".")[0]
        socket_name = f"X{suffix or '0'}"
        paths = (
            Path("/tmp/.X11-unix") / socket_name,
            Path("/mnt/wslg/.X11-unix") / socket_name,
        )
        return any(p.exists() for p in paths)
    # TCP/host-based DISPLAY values are accepted as-is.
    return True


def _resolve_firefox_browser_path_for_linux(path: str | None) -> str | None:
    """Map common wrapper paths to a native Firefox binary path when possible."""
    if not path:
        return path
    if sys.platform == "win32":
        return path
    lowered = path.replace("\\", "/").lower()
    if lowered in {"/usr/bin/firefox", "/snap/bin/firefox"}:
        native = Path("/snap/firefox/current/usr/lib/firefox/firefox")
        if native.is_file():
            return str(native)
    return path


def _ensure_geckodriver_available() -> str | None:
    """Return an actionable error when geckodriver is missing (common source of startup hangs)."""
    if sys.platform == "win32":
        return None
    if shutil.which("geckodriver"):
        return None
    return (
        "geckodriver was not found on PATH. Firefox headful startup may hang while Selenium tries to resolve it.\n"
        "Install geckodriver and retry (Ubuntu/WSL: `sudo apt-get install -y firefox-geckodriver` or "
        "`sudo apt-get install -y geckodriver`)."
    )


@dataclass(frozen=True)
class FetchSettings:
    user_agent: str
    timeout_seconds: float = 10.0
    initial_read_timeout_seconds: float = 5.0
    verbose: bool = False
    browser_path: str | None = None
    browser_engine: str = "chromium"
    prefer_open_browser: bool = False
    # Same as YP extract_from_all_pages: explicit CDP URL, e.g. http://127.0.0.1:9222
    cdp_endpoint: str | None = None
    # playwrong defaults to a very short socket probe; raise this so Edge on 9222 is detected.
    cdp_probe_timeout_seconds: float = 5.0

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
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
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
    """Browser fetcher backed by **playwrong** (``playwrong.sync_api.sync_playwright`` context manager)."""

    def __init__(self, settings: FetchSettings, *, headless: bool) -> None:
        self._settings = settings
        self._headless = headless

    def fetch(self, url: str, *, require_job_page: bool = False) -> FetchResult:
        method = FetchMethod.PLAYWRIGHT_HEADLESS if self._headless else FetchMethod.PLAYWRIGHT_HEADFUL
        try:
            from playwrong.sync_api import sync_playwright as playwrong_session
        except ImportError:
            return FetchResult(
                url=url,
                method=method,
                ok=False,
                error="playwrong is not installed. From the repo root: pip install -e ./playwrong",
            )

        if not self._headless:
            disp_err = _ensure_display_for_headful_unix()
            if disp_err:
                return FetchResult(url=url, method=method, ok=False, error=disp_err)
            if not _x_display_socket_ready():
                return FetchResult(
                    url=url,
                    method=method,
                    ok=False,
                    error=(
                        "Headful browser display appears unavailable: DISPLAY is set but no X socket was found "
                        "at /tmp/.X11-unix or /mnt/wslg/.X11-unix. Firefox launch would hang."
                    ),
                )

        content = ""
        status_code: int | None = None
        try:
            # prefer_open=True reuses an already-open debug Chromium/Edge when playwrong can reach the port.
            prefer_open = bool(self._settings.prefer_open_browser)
            cdp_url = (self._settings.cdp_endpoint or "").strip() or None
            probe_s = float(self._settings.cdp_probe_timeout_seconds)
            with playwrong_session(
                verbose=self._settings.verbose,
                prefer_open=prefer_open,
                cdp_timeout_seconds=probe_s,
                cdp_endpoints=[cdp_url] if cdp_url else None,
            ) as pw:
                launch_kwargs: dict[str, Any] = {"headless": self._headless, "prefer_open": prefer_open}
                engine = (self._settings.browser_engine or "chromium").strip().lower()
                if engine == "edge":
                    engine = "chromium"
                elif engine not in {"chromium", "firefox"}:
                    self._vlog("playwrong", f"unsupported browser engine '{engine}', falling back to chromium")
                    engine = "chromium"
                if engine == "firefox":
                    gecko_err = _ensure_geckodriver_available()
                    if gecko_err:
                        return FetchResult(url=url, method=method, ok=False, error=gecko_err)
                browser_path = self._settings.browser_path
                if engine == "firefox":
                    browser_path = _resolve_firefox_browser_path_for_linux(browser_path)
                if browser_path:
                    launch_kwargs["browser_path"] = browser_path
                self._vlog(
                    "playwrong",
                    f"launch {engine} headless={self._headless} browser_path={browser_path} "
                    f"prefer_open={prefer_open} cdp_endpoint={cdp_url!r}",
                )
                browser = None
                try:
                    if engine == "firefox":
                        browser = self._launch_with_timeout(pw.firefox.launch, launch_kwargs)
                    elif cdp_url:
                        # playwrong: explicit CDP HTTP endpoint (Edge/Chrome --remote-debugging-port).
                        browser = pw.chromium.connect_over_cdp(cdp_url, checkpoint_human=False)
                        self._vlog("playwrong", f"connect_over_cdp {cdp_url!r}")
                    else:
                        browser = self._launch_with_timeout(pw.chromium.launch, launch_kwargs)
                    context = browser.new_context(user_agent=self._settings.user_agent)
                    page = context.new_page()

                    try:
                        wait_until = "domcontentloaded" if require_job_page else "load"
                        response = page.goto(
                            url,
                            wait_until=wait_until,
                            timeout=int(self._settings.initial_read_timeout_seconds * 1000),
                        )
                        status_code = response.status if response else None
                    except Exception:
                        pass

                    if not require_job_page:
                        # Give SPA hydration a moment before we start scrolling/probing.
                        # Some Avature variants populate listings slightly after domcontentloaded.
                        self._wait_for_timeout(page, 1500)
                        self._auto_scroll_for_listings(page)
                    content = page.content()
                    if not require_job_page:
                        content = self._expand_jobs_menu_and_collect_pages(page, url, content)
                    probe = _probe_content(content, url, require_job_page=require_job_page, partial=True)
                    if probe.state == "bogus":
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
                            self._wait_for_load_state(page, "networkidle", int(remaining * 1000))
                            content = page.content()
                finally:
                    if browser is not None:
                        self._vlog("playwrong", "closing browser session")
                        try:
                            browser.close()
                        except Exception:
                            pass
        except Exception as exc:  # playwrong/CDP raises PlaywrongError and network errors.
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

    def _launch_with_timeout(self, launch_fn, launch_kwargs: dict[str, Any]):
        """Prevent indefinite browser-launch hangs (common with Firefox/geckodriver in WSL)."""
        timeout_s = max(10, int(self._settings.initial_read_timeout_seconds))
        if sys.platform == "win32":
            return launch_fn(**launch_kwargs)

        def _alarm_handler(_signum, _frame):
            raise TimeoutError(f"browser launch timed out after {timeout_s}s")

        previous = signal.getsignal(signal.SIGALRM)
        try:
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(timeout_s)
            return launch_fn(**launch_kwargs)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def _page_url(self, page) -> str:
        """playwrong CDP Page has .url; FirefoxPage (Selenium) does not — use current_url."""
        raw = getattr(page, "url", None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        driver = getattr(page, "_driver", None)
        if driver is not None:
            try:
                return str(getattr(driver, "current_url", "") or "")
            except Exception:
                pass
        return ""

    def _auto_scroll_for_listings(self, page) -> None:
        """Scroll until listing cards appear, or until bottom is reached."""
        try:
            self._vlog("playwrong", "auto-scroll start (landing page)")
            max_passes = 40
            for idx in range(max_passes):
                if self._page_has_epic_job_listing_cards(page):
                    self._vlog("scroll", f"cards detected before pass {idx + 1}; stopping scroll")
                    break
                before = self._page_eval(
                    page,
                    """
                    () => {
                      const nodes = Array.from(document.querySelectorAll('*'));
                      let best = null;
                      for (const n of nodes) {
                        if (!(n instanceof HTMLElement)) continue;
                        const style = window.getComputedStyle(n);
                        const overflowY = style.overflowY;
                        const scrollable = (overflowY === 'auto' || overflowY === 'scroll') && n.scrollHeight > (n.clientHeight + 8);
                        if (!scrollable) continue;
                        const score = n.clientHeight;
                        if (!best || score > best.score) {
                          best = { score, top: n.scrollTop, max: n.scrollHeight - n.clientHeight };
                        }
                      }
                      return {
                        y: window.scrollY,
                        h: document.body.scrollHeight,
                        vh: window.innerHeight,
                        containerTop: best ? best.top : null,
                        containerMax: best ? best.max : null
                      };
                    }
                    """
                )
                self._vlog(
                    "scroll",
                    f"pass {idx + 1}/{max_passes} before: y={before['y']}, h={before['h']}, "
                    f"containerTop={before['containerTop']}, containerMax={before['containerMax']}",
                )
                self._page_eval(
                    page,
                    """
                    () => {
                      const step = Math.max(500, Math.floor(window.innerHeight * 0.9));
                      // Scroll page itself.
                      window.scrollBy(0, step);
                      // Also scroll the largest scrollable container (if present).
                      const nodes = Array.from(document.querySelectorAll('*'));
                      let best = null;
                      for (const n of nodes) {
                        if (!(n instanceof HTMLElement)) continue;
                        const style = window.getComputedStyle(n);
                        const overflowY = style.overflowY;
                        const scrollable = (overflowY === 'auto' || overflowY === 'scroll') && n.scrollHeight > (n.clientHeight + 8);
                        if (!scrollable) continue;
                        const score = n.clientHeight;
                        if (!best || score > best.score) best = { node: n, score };
                      }
                      if (best && best.node) {
                        best.node.scrollBy(0, step);
                      }
                    }
                    """
                )
                self._wait_for_timeout(page, 220)
                after = self._page_eval(
                    page,
                    """
                    () => {
                      const nodes = Array.from(document.querySelectorAll('*'));
                      let best = null;
                      for (const n of nodes) {
                        if (!(n instanceof HTMLElement)) continue;
                        const style = window.getComputedStyle(n);
                        const overflowY = style.overflowY;
                        const scrollable = (overflowY === 'auto' || overflowY === 'scroll') && n.scrollHeight > (n.clientHeight + 8);
                        if (!scrollable) continue;
                        const score = n.clientHeight;
                        if (!best || score > best.score) {
                          best = { score, top: n.scrollTop, max: n.scrollHeight - n.clientHeight };
                        }
                      }
                      return {
                        y: window.scrollY,
                        h: document.body.scrollHeight,
                        vh: window.innerHeight,
                        containerTop: best ? best.top : null,
                        containerMax: best ? best.max : null
                      };
                    }
                    """
                )
                self._vlog(
                    "scroll",
                    f"pass {idx + 1}/{max_passes} after: y={after['y']}, h={after['h']}, "
                    f"containerTop={after['containerTop']}, containerMax={after['containerMax']}",
                )
                at_or_near_bottom = (after["y"] + after["vh"]) >= (after["h"] - 8)
                container_at_bottom = (
                    after["containerTop"] is not None
                    and after["containerMax"] is not None
                    and after["containerTop"] >= (after["containerMax"] - 8)
                )
                no_page_progress = after["y"] <= before["y"] and after["h"] <= before["h"]
                no_container_progress = (
                    before["containerTop"] is None
                    or after["containerTop"] is None
                    or after["containerTop"] <= before["containerTop"]
                )
                no_progress = no_page_progress and no_container_progress
                # If we couldn't detect any scrollable container (containerTop is None),
                # "no_progress" is ambiguous and can prematurely stop hydration on sites
                # that render listings after JS timers or in a container our heuristic
                # doesn't recognize.
                should_stop = (at_or_near_bottom and container_at_bottom) or (
                    no_progress and after["containerTop"] is not None
                )
                if should_stop:
                    self._vlog(
                        "scroll",
                        f"stop condition met (bottom={at_or_near_bottom and container_at_bottom}, "
                        f"no_progress={no_progress}, containerTop={after['containerTop']})",
                    )
                    break
            if not self._page_has_epic_job_listing_cards(page):
                # One final settle wait for late hydration before giving up.
                self._vlog("scroll", "cards still not found after scrolling; waiting final settle window")
                self._wait_for_timeout(page, 500)
            else:
                self._vlog("scroll", "cards found after scrolling")
            self._page_eval(
                page,
                """
                () => {
                  window.scrollTo(0, 0);
                  const nodes = Array.from(document.querySelectorAll('*'));
                  for (const n of nodes) {
                    if (!(n instanceof HTMLElement)) continue;
                    const style = window.getComputedStyle(n);
                    const overflowY = style.overflowY;
                    const scrollable = (overflowY === 'auto' || overflowY === 'scroll') && n.scrollHeight > (n.clientHeight + 8);
                    if (scrollable) n.scrollTop = 0;
                  }
                }
                """
            )
            self._vlog("playwrong", "auto-scroll done; viewport reset to top")
        except Exception:
            self._vlog("playwrong", "auto-scroll failed with exception")
            return

    def _expand_jobs_menu_and_collect_pages(self, page, _base_url: str, initial_content: str) -> str:
        """Expand the Jobs submenu and stitch submenu page HTML into one discovery document."""
        if self._page_has_epic_job_listing_cards(page) or _has_epic_job_listing_cards(initial_content):
            # Landing page already has the listing-card structure we want.
            self._vlog("discovery", "landing already contains listing cards; skipping Jobs submenu traversal")
            return initial_content

        combined_sections: list[str] = [initial_content]
        visited: set[str] = set()
        original_url = self._page_url(page)
        # Some sites render the submenu markup even if hover/click fails.
        # If hover fails, still attempt to extract submenu links from the DOM.
        if self._hover_jobs_menu(page):
            self._vlog("discovery", "hovered Jobs menuitem successfully")
        else:
            self._vlog(
                "discovery",
                "failed to hover Jobs menuitem; attempting submenu link extraction anyway",
            )

        resolved_urls = self._jobs_submenu_urls(page)
        if not resolved_urls:
            self._vlog("discovery", "failed to read Jobs submenu links; using landing content only")
            return initial_content
        self._vlog("discovery", f"Jobs submenu links discovered: {len(resolved_urls)}")

        for target_url in resolved_urls:
            if target_url in visited:
                continue
            visited.add(target_url)
            try:
                response = page.goto(
                    target_url,
                    wait_until="load",
                    timeout=int(self._settings.timeout_seconds * 1000),
                )
                if response is not None and response.status >= 400:
                    self._vlog("discovery", f"submenu page HTTP {response.status}: {target_url}")
                    continue
                self._wait_for_load_state(page, "networkidle", 2500)
                page_html = page.content()
                if self._page_has_epic_job_listing_cards(page) or _has_epic_job_listing_cards(page_html):
                    self._vlog("discovery", f"listing cards found on submenu page: {target_url}")
                    combined_sections.append(page_html)
                    continue
                if self._apply_links_lead_to_listing_cards(page, current_category_url=target_url):
                    self._vlog("discovery", f"listing cards found via Apply probe from: {target_url}")
                    combined_sections.append(page.content())
                    continue
                if _has_epic_apply_only_state(page_html) or self._page_has_epic_apply_links(page):
                    # Category has Apply CTA(s) but no listing cards after probing; treat as empty.
                    self._vlog("discovery", f"apply-only category skipped (no listing cards): {target_url}")
                    continue
            except Exception:
                self._vlog("discovery", f"submenu traversal failed for: {target_url}")
                continue

        if original_url and self._page_url(page) != original_url:
            try:
                page.goto(original_url, wait_until="load", timeout=int(self._settings.timeout_seconds * 1000))
            except Exception:
                pass
        self._vlog("discovery", f"combined discovery documents: {len(combined_sections)}")
        return "\n".join(combined_sections)

    def _page_has_epic_job_listing_cards(self, page) -> bool:
        try:
            card_count = self._dom_count(page, "div.p-6.mt-3.bg-white.rounded-xl")
            card_count += self._dom_count(page, "div.p-6.mt-3.bg-white.rounded-x1")
            if card_count > 0:
                return True
            return (
                self._dom_count(page, "p[id^='jobs_matching_positions_'][id$='_title']") > 0
                and self._dom_count(page, "a[href*='/Careers/FolderDetail/']") > 0
            )
        except Exception:
            return False

    def _page_has_epic_apply_links(self, page) -> bool:
        try:
            return self._dom_count(page, "a[href*='/Careers/RegisterMethod?folderId=']") > 0
        except Exception:
            return False

    def _apply_links_lead_to_listing_cards(self, page, *, current_category_url: str) -> bool:
        """Probe Apply CTAs. If they never produce listing cards, treat category as empty."""
        apply_urls = self._dom_hrefs(page, "a[href*='/Careers/RegisterMethod?folderId=']")
        if not apply_urls:
            self._vlog("apply-probe", "no apply links found on category page")
            return False
        self._vlog("apply-probe", f"apply links found: {len(apply_urls)} (probing up to 5)")

        seen: set[str] = set()
        for apply_url in apply_urls[:5]:
            if apply_url in seen:
                continue
            seen.add(apply_url)
            try:
                response = page.goto(
                    apply_url,
                    wait_until="load",
                    timeout=int(self._settings.timeout_seconds * 1000),
                )
                if response is not None and response.status >= 400:
                    self._vlog("apply-probe", f"apply url HTTP {response.status}: {apply_url}")
                    continue
                self._wait_for_load_state(page, "networkidle", 2500)
                if self._page_has_epic_job_listing_cards(page):
                    self._vlog("apply-probe", f"listing cards discovered via apply url: {apply_url}")
                    return True
            except Exception:
                self._vlog("apply-probe", f"apply probe failed for: {apply_url}")
                continue
            finally:
                try:
                    page.goto(
                        current_category_url,
                        wait_until="load",
                        timeout=int(self._settings.timeout_seconds * 1000),
                    )
                except Exception:
                    pass
        self._vlog("apply-probe", "apply probes completed; no listing cards discovered")
        return False

    def _wait_for_load_state(self, page, state: str, timeout_ms: int) -> None:
        try:
            if hasattr(page, "wait_for_load_state"):
                page.wait_for_load_state(state, timeout=timeout_ms)
                return
        except Exception:
            return

    def _wait_for_timeout(self, page, timeout_ms: int) -> None:
        try:
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(timeout_ms)
                return
        except Exception:
            pass
        time.sleep(max(0, timeout_ms) / 1000.0)

    def _hover_jobs_menu(self, page) -> bool:
        try:
            if hasattr(page, "locator"):
                page.locator("#secondarytoolbar_submenu_Jobs [role='menuitem']").first.hover(timeout=2500)
                return True
            result = self._page_eval(
                page,
                """
                () => {
                  const menu = document.querySelector("#secondarytoolbar_submenu_Jobs [role='menuitem']");
                  if (!menu) return false;
                  menu.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
                  menu.dispatchEvent(new FocusEvent("focus", { bubbles: true }));
                  return true;
                }
                """,
            )
            return bool(result)
        except Exception:
            return False

    def _jobs_submenu_urls(self, page) -> list[str]:
        urls = self._dom_hrefs(page, "#secondarytoolbar_submenu_contents_Jobs a[role='menuitem'][href]")
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped

    def _dom_hrefs(self, page, selector: str) -> list[str]:
        try:
            result = self._page_eval(
                page,
                f"""() => Array.from(document.querySelectorAll({selector!r}))
                      .map(el => (el instanceof HTMLAnchorElement ? el.href : (el.getAttribute("href") || "")))
                      .map(v => String(v || "").trim())
                      .filter(Boolean)""",
            )
            return [str(item) for item in (result or [])]
        except Exception:
            self._vlog("dom", f"failed to collect hrefs for selector: {selector}")
            return []

    def _dom_count(self, page, selector: str) -> int:
        try:
            result = self._page_eval(
                page,
                f"() => document.querySelectorAll({selector!r}).length",
            )
            return int(result or 0)
        except Exception:
            return 0

    def _page_eval(self, page, expression: str) -> Any:
        if hasattr(page, "evaluate"):
            return page.evaluate(expression)
        driver = getattr(page, "_driver", None)
        if driver is not None:
            # playwrong FirefoxPage uses selenium under the hood.
            return driver.execute_script(f"return ({expression})()")
        # playwrong Page exposes the underlying CDP session.
        session = getattr(page, "_session", None)
        if session is None:
            raise RuntimeError("page object does not support evaluate or CDP session eval")
        result = session.send(
            "Runtime.evaluate",
            {"expression": f"({expression})()", "returnByValue": True},
            timeout_seconds=max(5.0, self._settings.timeout_seconds),
        )
        return (((result or {}).get("result") or {}).get("value")) if isinstance(result, dict) else None

    def _vlog(self, scope: str, message: str) -> None:
        if not self._settings.verbose:
            return
        console.print(f"[dim][playwrong:{scope}] {message}[/]")


def _has_epic_job_listing_cards(content: str) -> bool:
    lowered = content.lower()
    has_title_marker = 'id="jobs_matching_positions_' in lowered and "_title" in lowered
    has_folder_links = "/careers/folderdetail/" in lowered
    return has_title_marker and has_folder_links


def _has_epic_apply_only_state(content: str) -> bool:
    lowered = content.lower()
    has_apply = ">apply<" in lowered and "/careers/registermethod?folderid=" in lowered
    has_cards = _has_epic_job_listing_cards(content)
    return has_apply and not has_cards


def _decode_chunks(chunks: list[bytes], encoding: str | None) -> str:
    raw = b"".join(chunks)
    return raw.decode(encoding or "utf-8", errors="replace") if raw else ""


def _probe_content(content: str, url: str, *, require_job_page: bool, partial: bool) -> ContentProbe:
    """Classify fetched content without waiting for the full timeout when it is clearly bad."""
    lowered = content.lower()
    if _has_epic_job_listing_cards(content):
        return ContentProbe("usable", "Epic listing cards were detected on the page.")
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
