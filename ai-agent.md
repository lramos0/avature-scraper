# ai-agent.md

This document is for AI coding agents (OpenAI, Claude, Cursor, etc.) working in this repository.

It explains what this project does, how data flows, where behavior is intentionally strict vs best-effort, and what to verify before/after code changes.

## 1) Project purpose

`avature-scraper` is an ethical scraper for Avature-hosted career sites. It:

- checks robots policy first
- discovers job-detail URLs from career landing pages
- fetches job pages with progressive methods
- extracts normalized job rows
- writes incremental JSON cache/report and CSV
- can upload rows to JobPool cache API
- supports parallel batch runs with one subprocess per domain

Core usage patterns:

- single-site CLI: `python -m avature_ethics_scraper.cli ...`
- batch launcher: `parallelize.py` (usually via `scripts/run_parallel.ps1` on Windows)

## 2) High-level architecture

Important files:

- `src/avature_ethics_scraper/cli.py`
  - single-target command entrypoint
  - cache behavior and output wiring
- `src/avature_ethics_scraper/scraper.py`
  - orchestration: landing fetch, discovery, per-job fetch ladder, row acceptance
- `src/avature_ethics_scraper/fetchers.py`
  - requests + browser fetchers
  - playwrong/CDP attach path
- `src/avature_ethics_scraper/extract.py`
  - URL discovery and row extraction heuristics
- `src/avature_ethics_scraper/output_spec.py`
  - default vs optional field requirements and best-effort behavior
- `src/avature_ethics_scraper/cache.py`
  - JSON cache/report read/write and sanitization
- `parallelize.py`
  - batch dispatcher, worker process lifecycle, aggregate `jobs.json` + `jobs.csv`
- `scripts/run_parallel.ps1`
  - Windows wrapper, cwd/PYTHONPATH safety, Ctrl+C process-tree stop

## 3) Browser/CDP model (playwrong)

This repo uses `playwrong`, not Microsoft Playwright package APIs directly, even though the API shape is similar.

The typical headful/CDP path:

1. worker receives `--cdp-endpoint http://127.0.0.1:9222`
2. fetcher opens playwrong session
3. `pw.chromium.connect_over_cdp(...)`
4. create a new target/tab
5. navigate and scrape content

Critical compatibility note:

- newer Edge remote debugging endpoints require `PUT /json/new?url=...`
- `GET /json/new?...` returns `405 Method Not Allowed`
- if you see repeated headful `405` errors, confirm the active `playwrong` installation uses `PUT` for target creation

## 4) Parallel batch behavior

`parallelize.py` launches one Python subprocess per domain and merges worker outputs into aggregate files.

Batch defaults and guarantees:

- aggregate outputs stay synchronized:
  - JSON: `jobs.json`
  - CSV: `jobs.csv`
- Ctrl+C should stop worker process trees
- dead CDP endpoints fail fast at startup
- worker mode is non-interactive:
  - cache prompt auto-continues
  - fallback "Press Enter" steps auto-continue

Environment variables injected to workers by batch:

- `AVATURE_CACHE_ACTION=continue`
- `AVATURE_NON_INTERACTIVE=1`
- `AVATURE_RELAX_LOCATION=1` (location may be missing on otherwise valid rows)

## 5) Field policy (very important)

Current semantics:

- default/core row acceptance is required (id/title/description/url, plus location unless relaxed by env)
- optional fields requested via flags (or `--all`) are best-effort and should not drop rows
- missing optional values should produce warnings/review flags, not hard rejection

`--all` intent:

- request broad JobDataPool-shaped fields
- escalate fetch attempts to improve completeness
- still keep valid rows even when some optional fields cannot be derived

## 6) Upload behavior

Single CLI command:

- default: CSV write + upload enabled
- disable upload with `--no-upload-to-jobpool`

Parallel batch (`parallelize.py`):

- default: upload enabled
- disable with `--no-upload`

Uploads target:

- `https://jobpool.live/api/scrape-cache` by default
- configurable by `JOBPOOL_SCRAPE_CACHE_URL`

## 7) Known operational pitfalls

1. Batch appears stuck at 0%
- this can be normal before first worker completion
- inspect a worker log in `parallel_logs/`

2. Edge "does nothing"
- may be opening work in new CDP targets/tabs, not original about:blank tab
- confirm worker log shows `# cdp-endpoint=...` and playwrong connection lines

3. 0 rows despite successful landing fetch
- discovery selectors may not match that tenant's DOM
- listing may hydrate late; inspect verbose logs and cached landing content (debug mode)

4. Windows encoding crashes
- avoid non-ASCII progress/status output in batch paths unless console encoding is guaranteed

5. Multiple playwrong installs
- Python may import playwrong from another checkout/path
- verify with:
  - `py -c "import playwrong, playwrong.cdp; print(playwrong.__file__); print(playwrong.cdp.__file__)"`

## 8) Debug checklist for agents

When user reports "no jobs", "edge idle", or "stuck":

1. Check CDP listeners:
  - port(s) are listening
  - `/json/version` returns `webSocketDebuggerUrl`
2. Check worker logs:
  - command line includes expected flags
  - whether blocked by prompt, selector miss, or fetch errors
3. Confirm active playwrong path and `/json/new` verb behavior
4. Verify discovery counts:
  - `discovered_job_urls` non-empty?
5. Verify row rejection reason:
  - missing required defaults vs optional fields
6. Verify aggregate files update:
  - `jobs.json` and `jobs.csv` in sync

## 9) Safe change guidelines

- Prefer focused fixes with clear observability (logs/error messages)
- Do not reintroduce interactive prompts in parallel workers
- Preserve cancel safety on Windows (Ctrl+C path)
- Keep ASCII-safe output in batch status lines
- Keep `--all` best-effort semantics (optional fields do not hard-fail rows)
- If changing discovery logic, verify at least one known-good domain and one known-problem domain

## 10) Quick commands

Single domain smoke test with CDP:

```powershell
.\scripts\run_parallel.ps1 --workers 1 --domain bloomberg.avature.net --browser-engine chromium --cdp-endpoint "http://127.0.0.1:9222" --delay 0 --max-jobs 5 --verbose
```

Two-worker batch with two Edge instances:

```powershell
.\scripts\run_parallel.ps1 --workers 2 --browser-engine chromium --cdp-endpoints "http://127.0.0.1:9222,http://127.0.0.1:9223" --all
```

Check which playwrong is imported:

```powershell
py -c "import playwrong, playwrong.cdp; print(playwrong.__file__); print(playwrong.cdp.__file__)"
```

