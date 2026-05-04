from avature_ethics_scraper.extract import parse_avature_inline_job_header


def test_parse_avature_standard_lead_line():
    line = "Bloomberg, Account Manager Location Sydney Business Area Sales and Client Service Ref # 10050768"
    got = parse_avature_inline_job_header(line + "\n\nMore job body here.")
    assert got["company_name"] == "Bloomberg"
    assert got["job_title"] == "Account Manager"
    assert got["location"] == "Sydney"
    assert got["business_area"] == "Sales and Client Service"
    assert got["requisition_id"] == "10050768"
    assert got["stripped_description"] == "More job body here."


def test_parse_avature_location_only_no_business_area():
    line = "Acme Corp, Widget Engineer Location Remote Ref # 999"
    got = parse_avature_inline_job_header(line)
    assert got["company_name"] == "Acme Corp"
    assert "Widget" in (got["job_title"] or "")
    assert got["location"] == "Remote"
    assert got["requisition_id"] == "999"
    assert got["stripped_description"] is None


def test_parse_avature_ref_not_at_end_of_collapsed_line():
    """Body is often one line: banner then Description & Requirements (Ref # not at $)."""
    blob = (
        "Streaming Transmissions Operator - Contract Location New York Business Area News and Media "
        "Ref # 10051267 Description & Requirements Bloomberg LP has built a significant global media business."
    )
    got = parse_avature_inline_job_header(blob)
    assert got["job_title"] == "Streaming Transmissions Operator - Contract"
    assert got["location"] == "New York"
    assert got["business_area"] == "News and Media"
    assert got["requisition_id"] == "10051267"
    assert got["company_name"] is None
    assert "Bloomberg LP has built" in (got["stripped_description"] or "")


def test_job_title_from_detail_url_slug():
    from avature_ethics_scraper.extract import _job_title_from_avature_detail_url

    t = _job_title_from_avature_detail_url(
        "https://bloomberg.avature.net/careers/JobDetail/Streaming-Transmissions-Operator-Contract/19445"
    )
    assert t is not None
    assert "Streaming" in t
    assert "Contract" in t
