"""The classification-report log and the mailto draft."""

import json
from urllib.parse import unquote

from redactor import feedback

FIELDS = (
    "schema", "timestamp", "app_version", "platform", "error_type", "text",
    "predicted_category", "corrected_category", "corrected_replacement",
    "source", "occurrences", "documents", "origin",
)


def test_reports_round_trip_as_jsonl(tmp_path):
    report = feedback.build_report(
        error_type="Person flagged as organization",
        text="JANE ELLEN SMITH",
        predicted_category="organization",
        corrected_category="person",
        source="ner",
        occurrences=3,
        documents=["pleading.docx"],
        origin="review",
    )
    path = tmp_path / "log" / "feedback.jsonl"
    feedback.log_report(report, path)
    feedback.log_report(report, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    for field in FIELDS:
        assert field in parsed
    assert parsed["text"] == "JANE ELLEN SMITH"
    assert parsed["predicted_category"] == "organization"
    assert parsed["corrected_category"] == "person"


def test_the_mailto_draft_is_addressed_and_encoded():
    report = feedback.build_report(
        error_type="Wrong category (other)",
        text="Smith & Associates",
        predicted_category="person",
        corrected_category="organization",
        origin="suggestions",
    )
    url = feedback.mailto_url(report)
    assert url.startswith(f"mailto:{feedback.REPORT_ADDRESS}?subject=")
    assert " " not in url
    # the & inside the flagged text must not read as a parameter separator
    assert "Smith%20%26%20Associates" in url
    body = unquote(url.split("&body=", 1)[1])
    assert "Review before sending" in body
    assert "Smith & Associates" in body


def test_a_huge_report_is_truncated_and_says_so():
    report = feedback.build_report(
        error_type="Other",
        text="X" * 5000,
        predicted_category="person",
        corrected_category="organization",
    )
    body = unquote(feedback.mailto_url(report).split("&body=", 1)[1])
    assert len(body) <= 1800
    assert "Truncated" in body
