from avature_ethics_scraper.urls import normalize_url, robots_url_for


def test_normalize_url_defragments_and_adds_path():
    assert normalize_url("https://Example.com#x") == "https://example.com/"


def test_robots_url_for():
    assert robots_url_for("https://example.com/a/b?x=1") == "https://example.com/robots.txt"
