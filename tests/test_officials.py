"""The bench, and the second pass over the name list.

Two rules this file exists to hold in place:

*   a judicial officer's name survives a run intact, in whatever layout the
    court printed it - and not at the cost of a party who shares a surname;
*   a name ticked in step 2 never appears as part of another ticked name,
    because that hands one person two pseudonyms.
"""

import pytest

from redactor import engine, names as _names, officials, patterns, pipeline, review
from redactor.engine import Settings
from redactor.mapping import MappingStore

# Every way a court actually prints an officer. Only the first of these was
# protected before: the inline allowlist wanted the title immediately in front
# of the name, and a signature block puts it immediately after.
LAYOUTS = {
    "title first": "IT IS SO ORDERED. Judge Amber M. Cordova",
    "colon": "Judge: Amber M. Cordova",
    "title after a comma": "AMBER M. CORDOVA, District Court Judge",
    "title on the next line": "AMBER M. CORDOVA\nDISTRICT COURT JUDGE",
    "signature block": ("BY THE COURT:\n\n______________________\n"
                        "Amber M. Cordova\nDistrict Court Judge"),
    "assigned judge": "Assigned Judge: Amber M. Cordova",
    "honorable": "Before the Honorable Amber M. Cordova, Third District Court",
    "signed": "BY THE COURT:\n/s/ Amber M. Cordova\n",
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_every_layout_a_court_uses_is_harvested(layout):
    found = officials.harvest(LAYOUTS[layout])
    assert [o.name.casefold() for o in found] == ["amber m. cordova"]


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_every_layout_is_also_shielded_inline(layout):
    """Belt and braces: the allowlist covers these without the harvest too."""
    text = LAYOUTS[layout]
    if "/s/" in text:
        pytest.skip("a signed block carries no title; the harvest is what covers it")
    spans = patterns.allowlist_spans(text)
    covered = " ".join(text[start:end] for start, end in spans)
    assert "cordova" in covered.casefold()


def test_prose_and_attorneys_are_not_mistaken_for_the_bench():
    assert officials.harvest("if the judge agrees we will proceed") == []
    assert officials.harvest("The petitioner, Jane Elizabeth Smith, filed today.") == []
    assert officials.harvest(
        "/s/ Karen Whitfield\nKaren Whitfield\nAttorney for Petitioner") == []


def test_a_client_first_name_no_longer_renames_the_judge():
    """The reported failure, encoded.

    A client called Amber puts "Amber" on the variant list. Anywhere the title
    is not adjacent - a later paragraph referring back to the order - that
    variant used to land inside the judge's name and sign the order "Mabel M.
    Cordova".
    """
    order = ("BY THE COURT:\n______________________\n"
             "Amber M. Cordova\nDistrict Court Judge\n")
    later = "Amber M. Cordova signed the order; Amber reviewed the file herself."

    def run(protected):
        store = MappingStore()
        store.add_person("Amber Nicole Reyes", source="name-list")
        settings = Settings(use_ner=False, protected_names=protected)
        return engine.apply_hits(later, engine.scan_text(later, store, settings))

    assert "Amber M. Cordova" not in run([]), "the bug this test exists for"

    terms = officials.protected_terms(officials.harvest(order),
                                      avoid=["Amber Nicole Reyes"]).terms
    fixed = run(terms)
    assert "Amber M. Cordova" in fixed, "the judge must survive intact"
    assert "Amber reviewed" not in fixed, "the client must still be anonymized"


def test_a_bare_later_reference_to_the_judge_is_covered():
    bench = officials.harvest("Judge Amber M. Cordova")
    terms = officials.protected_terms(bench).terms
    text = "The Cordova ruling of March 4, and Judge Cordova's findings, control."
    covered = [text[s:e] for s, e in patterns.allowlist_spans(text, terms)]
    assert any("Cordova" in span for span in covered)


def test_the_shield_does_not_bleed_into_longer_words():
    terms = officials.protected_terms(officials.harvest("Judge Amber M. Cordova")).terms
    assert patterns.allowlist_spans("The Cordovan rug was an asset.", terms) == []


def test_a_party_sharing_the_surname_beats_the_bench():
    """Shielding a shared surname would ship the client in every sentence."""
    bench = officials.harvest("Judge Amber M. Smith")
    protection = officials.protected_terms(bench, avoid=["Jane Elizabeth Smith"])
    assert "Amber M. Smith" in protection.terms
    assert "Smith" not in protection.terms
    assert protection.partial == ["Amber M. Smith"]
    assert protection.notes(), "the operator has to be told the shield is partial"


def test_a_judge_who_is_also_a_party_is_not_shielded_at_all():
    bench = officials.harvest("Judge Amber M. Reyes")
    protection = officials.protected_terms(bench, avoid=["Amber Reyes"])
    assert protection.terms == []
    assert protection.dropped == ["Amber M. Reyes"]


def test_a_short_surname_is_only_shielded_in_full():
    protection = officials.protected_terms(officials.harvest("Judge Ann Ng"))
    assert "Ann Ng" in protection.terms
    assert "Ng" not in protection.terms, "two letters would punch holes in the scan"


# ------------------------------------------------- the step 2 second pass --

def test_a_surname_ticked_beside_a_full_name_is_folded_in():
    entries = [("Jane Elizabeth Smith", "person"), ("Smith", "person")]
    kept, overlaps = review.resolve_overlaps(entries)
    assert kept == [("Jane Elizabeth Smith", "person")]
    assert [(o.inner, o.outer, o.merge) for o in overlaps] == [
        ("Smith", "Jane Elizabeth Smith", True)]


def test_the_fold_gives_both_spellings_one_pseudonym():
    store, _overlaps = review.build_store(
        [("Jane Elizabeth Smith", "person"), ("Smith", "person")])
    people = store.persons()
    assert len(people) == 1, "two entities would mean two surrogates"
    text = "Jane Elizabeth Smith, called Smith throughout, testified."
    out = engine.apply_hits(
        text, engine.scan_text(text, store, Settings(use_ner=False)))
    surname = people[0].surrogate.last
    assert out.count(surname) == 2, out


def test_an_ambiguous_surname_is_left_alone_and_reported():
    entries = [("Jane Elizabeth Smith", "person"),
               ("John Michael Smith", "person"), ("Smith", "person")]
    kept, overlaps = review.resolve_overlaps(entries)
    assert ("Smith", "person") in kept, "guessing which Smith would be worse"
    assert all(not o.merge for o in overlaps)
    assert len(review.overlap_notes(overlaps)) == 2


def test_forms_the_store_already_merges_are_not_reported_as_overlaps():
    assert _names.overlapping_names(["Jane Elizabeth Smith", "Jane Smith"]) == []
    assert _names.overlapping_names(["Maria Lopez", "Carlos Rivera"]) == []


def test_a_name_is_never_folded_across_types():
    """"Smith" as an organization is not the person Smith."""
    kept, _overlaps = review.resolve_overlaps(
        [("Jane Elizabeth Smith", "person"), ("Smith", "organization")])
    assert ("Smith", "organization") in kept


# --------------------------------------------------------- wired together --

def test_officers_are_harvested_from_a_real_document(sample_docx):
    bench = pipeline.collect_officials([sample_docx])
    names = {o.name.casefold() for o in bench}
    assert "amber m. cordova" in names
    assert "delia farnsworth" in names


def test_a_whole_docx_run_leaves_the_signing_judge_alone(sample_docx, tmp_path):
    """End to end, with the party whose first name collides with the judge's."""
    from redactor import docx_processor

    bench = pipeline.collect_officials([sample_docx])
    parties = ["Amber Nicole Reyes", "Jane Elizabeth Smith"]
    protection = officials.protected_terms(bench, avoid=parties)
    settings = Settings(use_ner=False, protected_names=list(protection.terms))

    store, _overlaps = review.build_store([(name, "person") for name in parties])
    out = tmp_path / "signed.docx"
    docx_processor.process(sample_docx, out, store, settings)
    body = "\n".join(docx_processor.extract_text(out).values())

    assert "Amber M. Cordova" in body
    assert "District Court Judge" in body
    assert "Delia Farnsworth" in body
    assert "Jane Elizabeth Smith" not in body, "the client must still be replaced"


def test_the_api_reports_the_bench(sample_docx):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from redactor import webapp

    webapp.SESSION = webapp.Session()
    with TestClient(webapp.app) as client:
        client.cookies.set(webapp.COOKIE, webapp.TOKEN)
        with open(sample_docx, "rb") as handle:
            client.post("/api/files", files=[("files", (sample_docx.name, handle))])

        job = client.post("/api/captions").json()["job"]
        result = _await(client, job)
        names = {o["name"].casefold() for o in result["officials"]}
        assert "amber m. cordova" in names
        assert any("Cordova" in term for term in result["protected"])

        body = {"options": {"ner": False, "ocr": False},
                "names": [{"name": "Jane Elizabeth Smith", "category": "person"},
                          {"name": "Smith", "category": "person"}]}
        review_result = _await(client, client.post("/api/review", json=body).json()["job"])
        assert [o["inner"] for o in review_result["overlaps"]] == ["Smith"]
        people = [e for e in review_result["entities"] if e["is_person"]]
        assert len(people) == 1, "the folded surname must not become its own entity"


def _await(client, job_id, timeout=60):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] == "done":
            return state["result"]
        if state["status"] == "error":
            raise AssertionError(state["error"])
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_the_report_names_what_it_shielded(tmp_path):
    settings = Settings(protected_names=["Amber M. Cordova", "Cordova"],
                        extra_allowlist=["Wasatch Front"])
    result = pipeline.RunResult(output_dir=tmp_path)
    path = pipeline.write_report(result, MappingStore(), settings,
                                 tmp_path / "report.txt")
    text = path.read_text()
    assert "LEFT UNTOUCHED ON PURPOSE" in text
    assert "Amber M. Cordova" in text
    assert "Wasatch Front" in text
