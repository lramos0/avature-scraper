# avature-scraper

Ethical, **robots.txt-first** CLI for Avature-hosted career sites. It is meant as a reference implementation: polite delays, incremental JSON cache, progressive fetch (HTTP → headless Chromium → headful Chromium), and explicit legal gates on disallowed URLs.

This README focuses on the **normal pip + single-run CLI workflow**.
For parallel/Windows operator flow, use `README.unhinged.md`.

## Agent Guide

If you are an AI coding assistant (Claude/OpenAI/Cursor/etc.), read `ai-agent.md` first. It is the deep technical guide for how this repo works end-to-end (architecture, fetch/discovery flow, batch semantics, CDP/playwrong behavior, and failure modes).

## Pip Tooling (Primary Workflow)

This project is packaged with `pyproject.toml` and is intended to be run from a pip-installed environment.

### 1) Create and activate a venv

```bash
python -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2) Install this repo in editable mode

```bash
pip install -e .
```

### 3) Install playwrong (CDP/browser backend)

Use the vendored local package in this repo:

```bash
pip install -e ./playwrong
```

### 4) Optional extras

```bash
pip install -e ".[dev]"
pip install -e ".[browser]"
python -m playwright install chromium
```

Notes:

- `.[dev]` is for tests/lint tooling.
- `.[browser]` installs Playwright dependencies used by fallback paths.
- Browser/CDP automation in this repo is routed through `playwrong`.
- There is intentionally no `requirements.txt` in this repo.

## Domain Inventory Scope (No Subdomain Scanner Included)

This repo does **not** include an automated Avature subdomain enumeration crawler/scanner.

What is included:

- `sample.csv` as a small starter list of Avature business hosts
- support for your own larger CSV (for batch runs, `parallelize.py` prefers `avature_subdomains.csv` when present)

What is not included:

- brute-force/passive DNS subdomain discovery engine
- hosted-tenant universe maintenance

For discovery inside a known host (not host enumeration), see:

- `parallelize.py --discover-only` (collects discovered job URLs from landing pages)
- `src/avature_ethics_scraper/fetchers.py` (landing/menu traversal and discovery probing)
- `src/avature_ethics_scraper/extract.py` (`extract_listing_discovery_signals`, pagination/job URL signal extraction)

For maintaining a broad Avature host inventory, use external data sources/services and feed results into CSV input for this repo. Common options include passive DNS / domain intelligence providers such as [WhoisXML API](https://whoisxmlapi.com/), certificate transparency tooling, and internal enterprise domain inventories.

## Features

| Area | Behavior |
|------|----------|
| **Robots** | Fetches and honors `robots.txt` before each host; disallowed URLs require a typed legal acknowledgement to proceed. |
| **Cache** | `--output` / `-o` JSON is both the final report and the resume cache (no separate `--resume`). |
| **Fetch ladder** | `requests` → Playwright headless → Playwright headful, with an interactive “press Enter to continue” step between tiers when the previous tier did not yield usable job HTML. |
| **Job rows** | Default columns: `id`, `title`, `description`, `location`, `url`. Optional JobDataPool-style fields via flags or `--all`. |
| **Field gates** | When optional columns are requested, missing values on an early tier force escalation to the next fetch method (same UX as non-job HTML). After headful, a best-effort inference pass may fill gaps and sets `needs_manual_review` on the row. |
| **CSV + JobPool** | By default, writes a JobPool-shaped CSV next to `--output` (`<stem>.csv`) when there is at least one job, and **POST**s listings to [jobpool.live](https://jobpool.live/) scrape-cache. Disable the POST with `--no-upload-to-jobpool`. |

## Install (Quick)

```bash
pip install -e .
pip install -e ./playwrong
```

## Basic run

```bash
avature-scraper https://company.avature.net/careers --output jobs.json
```

Pin explicit job detail URLs (repeatable):

```bash
avature-scraper https://company.avature.net/careers \
  --job-url "https://company.avature.net/careers/JobDetail/Example-Role/12345" \
  --output jobs.json
```

## Output JSON

Each saved job includes at least:

- **`id`** — Requisition-style id from the URL/body when possible; otherwise a stable `url-<sha256-prefix>` derived from the normalized listing URL.
- **`title`**, **`description`** (full cleaned visible text, capped for cache size; see `description_truncated`), **`location`**, **`url`**.
- **`needs_manual_review`** — Set when headful inference or ingest-date substitution was used; such rows are **not** dropped by the heuristic bogus filter on cache write.
- **`data_quality_warnings`** — Short operator-facing notes.

The report also includes `output_fields`: the column names implied by your CLI flags (defaults only, or the full JobDataPool-style list when using `--all`).

## Optional column flags (JobDataPool-shaped)

These turn on **extraction + completion checks** for the matching columns (missing values on requests/headless cause escalation to the next tier, same as garbage-page detection):

| Flag | Affects |
|------|---------|
| `--skills` | `skills` |
| `--ingestion-date` | `job_posted_date` (page) plus runtime `ingest_utc_date` / `ingest_utc_hour`; after headful, posted date may be copied from ingest with a warning. |
| `--education-requirements` | `education_requirements` |
| `--company-name` | `company_name` |

### `--all` (JobDataPool export mode)

`--all` is shorthand for enabling **all** of the flags above **and** expanding `output_fields` to the full JobDataPool-style column set defined in code (`JOBDATAPOOL_OUTPUT_FIELDS` in `models.py`), suitable for CSV export toward [jobdatapool.com](https://jobdatapool.com/) / JobPool pipelines.

```bash
avature-scraper https://company.avature.net/careers --all --output jobs.json
```

## CSV export and JobPool upload

**Default behavior:** if the run produced **at least one job**, the tool writes **`{output-stem}.csv`** beside your JSON report (UTF-8 BOM, JobPool-shaped headers) and **POST**s the listing payload to **`https://jobpool.live/api/scrape-cache`** (override with `JOBPOOL_SCRAPE_CACHE_URL`). The POST body is JSON listing objects, not the raw CSV file.

The CSV uses headers compatible with the public scrape cache (`REQUIRED_FIELDS` in the [livejobpool](https://github.com/) Netlify function), including `source_observed_utc` and `record_lifecycle_state` (left blank here; the API normalizes them).

**Skip only the network upload** (still write the default CSV when there are jobs):

```bash
avature-scraper https://company.avature.net/careers --output jobs.json --no-upload-to-jobpool
```

**Custom CSV path** (upload still on unless you add `--no-upload-to-jobpool`):

```bash
avature-scraper https://company.avature.net/careers --output jobs.json --csv-out ./out/listings.csv
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `JOBPOOL_SCRAPE_CACHE_URL` | Override API URL (default `https://jobpool.live/api/scrape-cache`). |
| `LIVEJOBPOOL_ROOT` | Absolute path to your [livejobpool](https://github.com/) clone. When set, each upload payload is **also** appended as one JSON line to `{LIVEJOBPOOL_ROOT}/.avature-scraper-cache/<user>.jsonl` for a local audit trail keyed by the resolved user name. |

Example (Windows PowerShell):

```powershell
$env:LIVEJOBPOOL_ROOT = "C:\Users\you\Projects\livejobpool"
avature-scraper https://company.avature.net/careers --output jobs.json
```

### Who is `user_name` on the wire?

The cache API expects `user_name` for leaderboards. Resolution order:

1. `--jobpool-user` if you passed it  
2. `git config github.user`  
3. GitHub handle parsed from `git config remote.origin.url` (or `upstream`) when it looks like `git@github.com:handle/repo.git` or `https://github.com/handle/repo`  
4. Slugified `git config user.name`  
5. OS login (`getpass.getuser()`)  
6. Machine hostname  

## Output cache behavior (JSON)

If `--output` already exists, you get a small TTY selector: continue from cache or overwrite. The JSON is written **incrementally** after discovery and after each successful job fetch.

Raw landing HTML is **not** kept in the cache file on disk (only enough for discovery resume).

## Legal gate

If `robots.txt` disallows a URL, the tool stops unless you type the exact acknowledgement phrase shown in the UI. This is deliberate friction; the tool does not provide legal advice.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q
ruff check src tests
```

## Related projects

- **[jobpool.live](https://jobpool.live/)** — transparency layer and public scrape-cache API used above.  
- **[jobdatapool.com](https://jobdatapool.com/)** — schema / interchange expectations for job rows.  
- **livejobpool** — Netlify site + `cache-scraped-listings` function backing the cache.
