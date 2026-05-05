# playwrong

`playwrong` is a custom browser automation project for this repo.

It does not import Playwright.
It includes a local `undetected_geckodriver`-compatible package inside this project.

## What it currently supports

- Chromium-family automation via CDP WebSocket.
- Firefox automation via local `undetected_geckodriver` wrapper (Selenium-based).
- Detect and prefer attaching to an already-open debug browser first.
- Launch a new debug browser if no open one is available.
- Minimal Playwright-like sync API surface for scraping flows:
  - `sync_playwright()`
- `p.chromium.launch(...)`
- `p.firefox.launch(...)`
  - `browser.new_page()`
  - `page.goto(url)`
  - `page.content()`
  - `browser.close()`

## Install (editable)

```bash
cd playwrong
python -m pip install -e .
```

## Usage

```python
from playwrong.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.firefox.launch(headless=False, checkpoint_human=True)
    page = browser.new_page()
    # pauses for manual intervention before navigation when checkpoint_human=True
    page.goto("https://example.com")
    # explicit manual checkpoint anywhere in the script
    page.checkpoint("Solve captcha manually, then press Enter")
    html = page.content()
    browser.close()
```

## Local Undetected Gecko Driver

This repo also provides a local package at `undetected_geckodriver`:

```python
from undetected_geckodriver import Firefox

driver = Firefox()
driver.get("https://www.example.com")
```

## Notes

- Open-browser attach requires a Chromium browser started with remote debugging, for example:
  `--remote-debugging-port=9222`
- Firefox backend launches a new WebDriver session; attach-to-existing-window is not available there.
