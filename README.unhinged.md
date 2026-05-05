# README.unhinged

This is the "do not make me think" guide.

This README is intentionally about **parallelization operations** (multi-worker runs, CDP endpoints, cancellation, and recovery).

For AI coding assistants (Claude/OpenAI/Cursor): read `ai-agent.md` first. It is the deep, complete technical guide for this repo.

If you want standard pip install + single CLI usage, see `README.md`.

Goal:

- Open Microsoft Edge with CDP debug port (same pattern as YP Service Processor)
- Run Avature parallel scraper on 1–2+ workers safely
- Keep it cancel-safe with `Ctrl+C`

## Parallelization-first quick start

```powershell
cd "C:\Users\logan\Projects\avature-scraper"
py -m pip install -e .
py -m pip install -e ./playwrong
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\edge-cdp-profile" about:blank
.\scripts\run_parallel.ps1 --workers 1 --browser-engine chromium --cdp-endpoint "http://127.0.0.1:9222"
```

For two workers, start a second Edge instance on `9223` with a different `--user-data-dir`, then run with:

```powershell
.\scripts\run_parallel.ps1 --workers 2 --browser-engine chromium --cdp-endpoints "http://127.0.0.1:9222,http://127.0.0.1:9223"
```

## 0) Prereqs

- Run from PowerShell on Windows
- `py` is available (Python 3.10+)
- Repo path (adjust if yours differs):
  - `C:\Users\logan\Projects\avature-scraper`
- Domain list input:
  - this repo does **not** scan the internet for Avature subdomains
  - `sample.csv` is a small starter list
  - for large runs, provide/maintain your own `avature_subdomains.csv`
  - if you need host inventory sources, use external providers (for example [WhoisXML API](https://whoisxmlapi.com/)) and feed those hosts into CSV

## 1) Open Edge with debug port (YP-style)

Close other Edge windows if you want a clean profile, then start a dedicated debug instance:

```powershell
Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\edge-cdp-profile" about:blank
```

If Edge lives under `Program Files` (64-bit path), use that `msedge.exe` instead.

Leave that Edge window open.

**What you should see when scraping:** automation uses **playwrong** (this repo’s `playwrong/` package — not `pip install playwright`). It attaches over CDP and opens **new tabs**; your original `about:blank` tab may sit idle while work happens in another tab. Use `--verbose` on the batch if you want `[playwrong:…]` lines in the console.

## 2) Install once (from repo root)

There is no `requirements.txt` in this repo; install the project and vendored playwrong:

```powershell
cd "C:\Users\logan\Projects\avature-scraper"
py -m pip install -e .
py -m pip install -e ./playwrong
```

(If you maintain playwrong elsewhere, you can `pip install -e` that path instead — but `./playwrong` in this repo is the supported copy.)

## 3) Run the batch (cwd vs full path)

PowerShell only resolves `.\scripts\run_parallel.ps1` if your **current directory** is the repo root. If you see *The term ... is not recognized*, you are in the wrong folder.

**Option A — `cd` first:**

```powershell
cd "C:\Users\logan\Projects\avature-scraper"
```

**Option B — full path from anywhere** (note the leading `&`):

```powershell
& "C:\Users\logan\Projects\avature-scraper\scripts\run_parallel.ps1" --workers 2 --browser-engine chromium --cdp-endpoints "http://127.0.0.1:9222,http://127.0.0.1:9223"
```

The script `cd`s to the repo internally before running `parallelize.py`, so Option B works even if your prompt is elsewhere.

**If you get “running scripts is disabled”:**

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### `--browser-engine`

Browser automation is **always** playwrong. For CDP attach to Edge/Chrome, use `chromium` (default) or `edge` — both use playwrong’s Chromium-class CDP path. `firefox` is a separate code path (not your Edge CDP window).

## 4) Two workers without fighting one Edge (recommended)

One `--remote-debugging-port` = **one Edge process**. Workers sharing the same `http://127.0.0.1:9222` share that process; playwrong opens **new tabs** there. That often works, but two concurrent workers can still step on each other.

**Clean fix:** two Edge instances on **two ports** and pass both URLs; the batch **round-robins** jobs (`--cdp-endpoints` comma-separated).

Window A (port 9222):

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\edge-cdp-profile-a" about:blank
```

Window B (port 9223, **different** `--user-data-dir`):

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9223 --remote-allow-origins=* --user-data-dir="$env:TEMP\edge-cdp-profile-b" about:blank
```

Run (2 workers, 2 CDP URLs):

```powershell
.\scripts\run_parallel.ps1 --workers 2 --browser-engine chromium --cdp-endpoints "http://127.0.0.1:9222,http://127.0.0.1:9223" --delay 3 --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
```

Each worker log under `parallel_logs/` starts with `# cdp-endpoint=...` for the URL that subprocess used.

### One Edge only (single port)

One worker is simplest:

```powershell
.\scripts\run_parallel.ps1 --workers 1 --browser-engine chromium --cdp-endpoint "http://127.0.0.1:9222" --delay 3 --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
```

You can run `--workers 2` with a single `--cdp-endpoint` if you accept shared-browser risk.

**CDP + fetch tiers:** With `--cdp-endpoint` / `--cdp-endpoints` set, job-detail fetches **always** start with the headful playwrong tier so CDP stays in the loop (no “HTTP wins once, then skip the browser” for later jobs). If headful still fails, the scraper falls back to HTTP/headless as before.

**YP parity:** these flags map to playwrong’s `connect_over_cdp` (explicit URL), same idea as YP’s `extract_from_all_pages.py --cdp-endpoint "http://127.0.0.1:9222"`.

## 5) Optional: one domain first (smoke test)

```powershell
.\scripts\run_parallel.ps1 --workers 1 --domain xerox.avature.net --browser-engine chromium --cdp-endpoint "http://127.0.0.1:9222" --delay 3 --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
```

## 6) How to stop immediately

1. **Preferred:** in the PowerShell where you started the scrape, press **`Ctrl+C` once** and wait a few seconds.  
   `run_parallel.ps1` handles console cancel and tries to tear down the `parallelize.py` process tree; `parallelize.py` also stops worker subprocesses cooperatively.

2. **If a worker is still stuck:** last resort — this matches **any** command line containing these strings (can affect other terminals’ scrapes):

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'parallelize.py|avature_ethics_scraper|avature-scraper' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

3. **Close debug Edge:** Task Manager, or (closes **all** Edge windows):

```powershell
Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
```

## 7) If you still get 403/406

Try slower, single worker:

```powershell
.\scripts\run_parallel.ps1 --workers 1 --delay 5 --browser-engine chromium --cdp-endpoint "http://127.0.0.1:9222" --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
```

Then add a second Edge on port 9223 and use `--cdp-endpoints "http://127.0.0.1:9222,http://127.0.0.1:9223"` with `--workers 2` so workers stay separated.
