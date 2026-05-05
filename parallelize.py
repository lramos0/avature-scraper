"""Run many career-site scrapes in parallel (one subprocess per target).

Default CSV: uses avature_subdomains.csv in the current directory if it exists, else sample.csv.

By default each worker runs ``python -u -m avature_ethics_scraper.cli`` so log files grow while the
job runs. Use ``--path-scraper`` if you need the ``avature-scraper`` console script instead.

Examples:
  py parallelize.py --workers 4
  py parallelize.py --csv avature_subdomains.csv --workers 2 --all --delay 3
  py parallelize.py --workers 2 --browser-engine firefox --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console

try:
    from avature_ethics_scraper.fetchers import wslg_display_env_for_subprocess
except ImportError:

    def wslg_display_env_for_subprocess() -> dict[str, str]:
        if sys.platform == "win32":
            return {}
        if os.environ.get("DISPLAY", "").strip() or os.environ.get("WAYLAND_DISPLAY", "").strip():
            return {}
        if Path("/mnt/wslg").is_dir():
            return {"DISPLAY": ":0"}
        return {}
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text


def _safe_name(value: str) -> str:
    return (
        value.replace("https://", "")
        .replace("http://", "")
        .replace("/", "_")
        .replace(":", "_")
    )


def _default_csv_path() -> Path:
    preferred = Path("avature_subdomains.csv")
    if preferred.is_file():
        return preferred
    return Path("sample.csv")


def _load_domains(path: Path) -> list[str]:
    import csv

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["domain"].strip() for row in reader if row.get("domain")]


def _is_mail_or_smtp_host(domain: str) -> bool:
    """Skip obvious mail / SMTP integration hosts (not career sites)."""
    host = domain.strip().lower().split("/")[0].split(":")[0]
    if not host:
        return False
    for label in host.split("."):
        if label.startswith("smtp") or label.startswith("mail"):
            return True
        if label in {"mx", "mx1", "mx2", "postfix", "imap", "pop", "pop3"}:
            return True
    return False


def _filter_domains(domains: list[str], *, skip_mail_hosts: bool) -> tuple[list[str], list[str]]:
    if not skip_mail_hosts:
        return domains, []
    kept: list[str] = []
    skipped: list[str] = []
    for d in domains:
        if _is_mail_or_smtp_host(d):
            skipped.append(d)
        else:
            kept.append(d)
    return kept, skipped


def _build_scraper_prefix(args: argparse.Namespace) -> list[str]:
    """Prefer ``python -u -m`` so child stdout is unbuffered when redirected to a log file."""
    if args.path_scraper:
        return [args.scraper_exe]
    return [sys.executable, "-u", "-m", "avature_ethics_scraper.cli"]


class _CatMoodColumn(ProgressColumn):
    """Rotating cat emoji so long batch runs feel less bleak."""

    def render(self, task: Task) -> Text:
        cats = ("🐱", "😺", "😸", "😻", "😽")
        frame = int(time.monotonic() * 2) % len(cats)
        return Text(cats[frame])


class _FirstFinishWaitColumn(ProgressColumn):
    """Shows a ticking clock at 0%% — `as_completed` yields nothing until the first subprocess exits."""

    def __init__(self, t0: float, workers: int) -> None:
        self._t0 = t0
        self._workers = workers

    def render(self, task: Task) -> Text:
        if task.completed >= 1:
            return Text("")
        secs = int(time.monotonic() - self._t0)
        return Text(
            f"{secs}s @ {self._workers} workers — bar stuck at 0% until 1st browser scrape exits (often 1–5+ min)",
            style="dim italic",
        )


def run_one(domain: str, args: argparse.Namespace, out_dir: Path, log_dir: Path) -> dict:
    out_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    name = _safe_name(domain)
    output = out_dir / f"{name}.json"
    log_path = log_dir / f"{name}.log"

    url = args.url_pattern.format(domain=domain)
    prefix = _build_scraper_prefix(args)
    cmd: list[str] = [
        *prefix,
        url,
        "--output",
        str(output),
        "--timeout",
        str(args.timeout),
        "--max-jobs",
        str(args.max_jobs),
    ]
    if args.no_upload:
        cmd.append("--no-upload-to-jobpool")
    if args.angry:
        cmd.append("--angry")
    if args.verbose:
        cmd.append("--verbose")
    if args.browser_engine:
        cmd.extend(["--browser-engine", args.browser_engine])
    if args.browser_path:
        cmd.extend(["--browser-path", args.browser_path])
    if args.all:
        cmd.append("--all")
    if args.delay is not None:
        cmd.extend(["--delay", str(args.delay)])
    if args.allow_external_hosts:
        cmd.append("--allow-external-hosts")
    if args.allow_disallowed_robots:
        cmd.append("--allow-disallowed-robots")
    if args.headful_for_each_job:
        cmd.append("--headful-for-each-job")
    if args.discover_only:
        cmd.append("--discover-only")

    started = time.monotonic()
    child_env = {
        **os.environ,
        **wslg_display_env_for_subprocess(),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    # Binary unbuffered log + Popen without text=: on Windows, text-mode redirection to a
    # file often block-buffers; parallel logs then look empty until the worker exits.
    with log_path.open("wb", buffering=0) as log:
        def _b(msg: str) -> None:
            log.write(msg.encode("utf-8"))

        _b(f"# started {time.strftime('%Y-%m-%d %H:%M:%S')} domain={domain}\n")
        _b("# " + " ".join(cmd) + "\n")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=child_env,
            )
        except OSError as exc:
            _b(f"# spawn failed: {exc!r}\n")
            return {
                "domain": domain,
                "returncode": 127,
                "elapsed": time.monotonic() - started,
                "output": str(output),
                "log": str(log_path),
                "error": f"spawn failed: {exc}",
            }
        _b(f"# pid={proc.pid}\n")
        while True:
            code = proc.poll()
            if code is not None:
                elapsed = time.monotonic() - started
                return {
                    "domain": domain,
                    "returncode": code,
                    "elapsed": elapsed,
                    "output": str(output),
                    "log": str(log_path),
                }
            elapsed = time.monotonic() - started
            if elapsed > args.hard_timeout:
                proc.kill()
                proc.wait()
                return {
                    "domain": domain,
                    "returncode": -9,
                    "elapsed": elapsed,
                    "output": str(output),
                    "log": str(log_path),
                    "error": "hard timeout killed process",
                }
            time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parallel Avature career scrapes (subprocess per domain).")
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="CSV with a 'domain' column (default: avature_subdomains.csv if present, else sample.csv)",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="Run a single explicit domain (bypasses CSV ordering), e.g. --domain epic.avature.net",
    )
    parser.add_argument(
        "--max-domains",
        type=int,
        default=None,
        help="Limit number of domains dispatched after filtering (useful for fast smoke tests).",
    )
    parser.add_argument("--workers", type=int, default=4, help="Concurrent subprocesses")
    parser.add_argument("--out-dir", type=Path, default=Path("parallel_outputs"))
    parser.add_argument("--log-dir", type=Path, default=Path("parallel_logs"))
    parser.add_argument(
        "--url-pattern",
        default="https://{domain}/careers",
        help="Target URL pattern; must include {domain}",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Passed to scraper --timeout")
    parser.add_argument("--max-jobs", type=int, default=25, help="Passed to scraper --max-jobs")
    parser.add_argument(
        "--hard-timeout",
        type=int,
        default=30 * 60,
        help="Kill subprocess after this many seconds (wall clock)",
    )
    parser.add_argument("--angry", action="store_true", help="Pass --angry to scraper")
    parser.add_argument("--verbose", action="store_true", help="Pass --verbose to scraper")
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Allow JobPool upload (default: pass --no-upload-to-jobpool to each scraper)",
    )
    parser.add_argument(
        "--scraper-exe",
        default="avature-scraper",
        help="With --path-scraper only: executable name on PATH (default: avature-scraper)",
    )
    parser.add_argument(
        "--path-scraper",
        action="store_true",
        help=(
            "Run scraper from PATH (e.g. avature-scraper). Default is instead "
            f"{sys.executable} -u -m avature_ethics_scraper.cli for unbuffered log output when redirecting to files."
        ),
    )
    parser.add_argument(
        "--module",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--browser-engine", default=None, help="e.g. chromium or firefox")
    parser.add_argument("--browser-path", default=None, help="Browser binary path for playwrong")
    parser.add_argument(
        "--no-skip-mail-hosts",
        action="store_true",
        help="Scrape all CSV rows including smtp-* / mail-* / mx-style hosts (default: skip those)",
    )
    parser.add_argument(
        "--list-skipped-mail-hosts",
        action="store_true",
        help="Print every mail/smtp-skipped host (default: one summary line; large CSVs spam hundreds of lines)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Pass --all to each scraper (JobDataPool-style columns; stricter extraction, more fallbacks)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Polite delay between job-detail requests per subprocess (scraper --delay); recommended with --all",
    )
    parser.add_argument(
        "--allow-external-hosts",
        action="store_true",
        help="Pass --allow-external-hosts when career pages link job URLs on other hosts",
    )
    parser.add_argument(
        "--respect-robots",
        action="store_true",
        help=(
            "Do not pass --allow-disallowed-robots. Many *.avature.net tenants disallow /careers for crawlers; "
            "without the flag, non-interactive runs abort at the legal gate (exit 1)."
        ),
    )
    parser.add_argument(
        "--headful-for-each-job",
        action="store_true",
        help=(
            "Pass --headful-for-each-job: always open headful Playwright for every job URL "
            "(default skips headful after HTTP succeeds once per process)."
        ),
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Pass --discover-only to workers: gather discovered job URLs only, skip detail page scraping.",
    )
    parser.add_argument(
        "--discovered-urls-out",
        type=Path,
        default=Path("discovered_job_urls.txt"),
        help="When --discover-only is set, write deduped discovered job URLs here.",
    )
    parser.add_argument(
        "--plain-progress",
        action="store_true",
        help="Disable the Rich cat progress bar (use one-line stderr updates every 25 domains instead).",
    )
    args = parser.parse_args()
    args.no_upload = not args.upload
    skip_mail = not args.no_skip_mail_hosts
    # Many Avature hosts disallow /careers for the default UA; batch subprocesses have no TTY for the legal gate.
    args.allow_disallowed_robots = not args.respect_robots

    csv_path = args.csv if args.csv is not None else _default_csv_path()
    raw_domains = _load_domains(csv_path)
    domains, skipped_mail = _filter_domains(raw_domains, skip_mail_hosts=skip_mail)
    if args.domain:
        domains = [args.domain.strip()]
    elif args.max_domains is not None and args.max_domains > 0:
        domains = domains[: args.max_domains]
    print(
        f"CSV {csv_path} — {len(raw_domains)} row(s), {len(domains)} after mail/smtp filter "
        f"({len(skipped_mail)} skipped)"
    )
    if args.allow_disallowed_robots:
        print(
            "Subprocesses will use --allow-disallowed-robots when robots.txt disallows the URL "
            "(required for non-interactive parallel runs). Use --respect-robots to disable."
        )
    if skipped_mail:
        if args.list_skipped_mail_hosts:
            for d in skipped_mail:
                print(f"[SKIP] {d} (mail/smtp-style host)")
        else:
            sample = ", ".join(skipped_mail[:3])
            more = f" (+{len(skipped_mail) - 3} more)" if len(skipped_mail) > 3 else ""
            print(
                f"[SKIP] {len(skipped_mail)} mail/smtp-style host(s){more}; e.g. {sample}. "
                f"Use --list-skipped-mail-hosts for the full list."
            )
    if not domains:
        print(f"No domains to run after filters (csv={csv_path})", file=sys.stderr)
        sys.exit(2)

    args.out_dir.mkdir(exist_ok=True)
    args.log_dir.mkdir(exist_ok=True)

    print(
        f"Dispatching {len(domains)} scrape(s) with {max(1, args.workers)} worker(s). "
        f"Logs -> {args.log_dir.resolve()} | outputs -> {args.out_dir.resolve()}",
        flush=True,
    )
    print(
        "🐱 Progress stays at 0% until the first site finishes (Firefox + --all is slow). "
        f"Pick any file under the log dir and: tail -f <that>.log",
        flush=True,
    )
    wslg_disp = wslg_display_env_for_subprocess()
    if wslg_disp:
        print(
            f"WSLg: workers inherit {wslg_disp} (DISPLAY was unset here) so headful browsers can open.",
            flush=True,
        )

    total = len(domains)
    use_rich_bar = sys.stderr.isatty() and not args.plain_progress
    n_ok = n_fail = n_timeout = 0
    discovered: list[str] = []
    discovered_seen: set[str] = set()
    if args.discover_only:
        args.discovered_urls_out.parent.mkdir(parents=True, exist_ok=True)
        args.discovered_urls_out.write_text("", encoding="utf-8")

    def _ingest_discovered_for_result(result: dict, progress: Progress | None) -> None:
        if not args.discover_only:
            return
        report_path = Path(result["output"])
        try:
            raw = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        urls = raw.get("discovered_job_urls", [])
        if not isinstance(urls, list):
            return
        new_urls: list[str] = []
        for value in urls:
            if not isinstance(value, str):
                continue
            url = value.strip()
            if not url or url in discovered_seen:
                continue
            discovered_seen.add(url)
            discovered.append(url)
            new_urls.append(url)
        if not new_urls:
            return
        with args.discovered_urls_out.open("a", encoding="utf-8") as out:
            out.write("\n".join(new_urls) + "\n")
        msg = (
            f"[DISCOVER] +{len(new_urls)} from {result['domain']} "
            f"(total={len(discovered)}) -> {args.discovered_urls_out}"
        )
        if progress is not None:
            progress.console.log(f"[cyan]{msg}[/]")
        else:
            print(msg, flush=True)

    def _handle_result(result: dict, progress: Progress | None) -> None:
        nonlocal n_ok, n_fail, n_timeout
        d = result["domain"]
        log = result["log"]
        elapsed = result["elapsed"]
        if result.get("error"):
            n_timeout += 1
            msg = f"[TIMEOUT] {d} {elapsed:.1f}s {result['error']} log={log}"
            if progress is not None:
                progress.console.log(f"[yellow]{msg}[/]")
            else:
                print(msg, flush=True)
        elif result["returncode"] == 0:
            n_ok += 1
        else:
            n_fail += 1
            msg = f"[FAIL] {d} code={result['returncode']} {elapsed:.1f}s log={log}"
            if progress is not None:
                progress.console.log(f"[red]{msg}[/]")
            else:
                print(msg, flush=True)
        _ingest_discovered_for_result(result, progress)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(run_one, d, args, args.out_dir, args.log_dir): d for d in domains}
        if use_rich_bar:
            console = Console(stderr=True)
            batch_t0 = time.monotonic()
            workers_n = max(1, args.workers)
            with Progress(
                SpinnerColumn(),
                _CatMoodColumn(),
                TextColumn("[bold bright_magenta]{task.description}[/]"),
                _FirstFinishWaitColumn(batch_t0, workers_n),
                BarColumn(bar_width=32),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=False,
                refresh_per_second=12,
            ) as progress:
                task_id = progress.add_task(
                    f"🐱 0/{total} — not stuck; subprocesses are running",
                    total=total,
                )
                for n_done, future in enumerate(as_completed(futures), start=1):
                    result = future.result()
                    short = result["domain"][:52] + "…" if len(result["domain"]) > 52 else result["domain"]
                    progress.update(task_id, description=f"last: {short}")
                    progress.advance(task_id)
                    _handle_result(result, progress)
        else:
            print(
                "🐱 Plain progress: first line appears when the first site finishes (often minutes). "
                f"Logs: {args.log_dir.resolve()}/",
                file=sys.stderr,
                flush=True,
            )
            for n_done, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                _handle_result(result, None)
                if n_done == 1 or n_done % 25 == 0 or n_done == total:
                    print(
                        f"🐱 progress {n_done}/{total}  (ok={n_ok} fail={n_fail} timeout={n_timeout})",
                        file=sys.stderr,
                        flush=True,
                    )

    print(
        f"🐱 batch done: {n_ok} ok, {n_fail} fail, {n_timeout} timeout / {total} total",
        flush=True,
    )
    if args.discover_only:
        print(
            f"Discover-only aggregation: wrote {len(discovered)} unique job URL(s) to {args.discovered_urls_out.resolve()}",
            flush=True,
        )


if __name__ == "__main__":
    main()
