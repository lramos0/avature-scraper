from avature_ethics_scraper.git_identity import resolve_cache_user_key


def test_resolve_cache_user_key_returns_non_empty_string():
    key = resolve_cache_user_key()
    assert isinstance(key, str)
    assert len(key) >= 1
