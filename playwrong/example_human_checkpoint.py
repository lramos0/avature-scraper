from playwrong.sync_api import sync_playwright


def main() -> None:
    with sync_playwright(prefer_open=True, verbose=True) as p:
        browser = p.firefox.launch(headless=False, checkpoint_human=True)
        page = browser.new_page()
        page.goto("https://example.com")
        page.checkpoint("Interact manually in the browser, then press Enter here to continue.")
        html = page.content()
        print(f"Fetched HTML length: {len(html)}")
        browser.close()


if __name__ == "__main__":
    main()
