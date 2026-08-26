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


# Commissioners carry most of the signing in domestic practice, and their
# blocks are laid out differently from a judge's - the heading names the role
# and the name below it often has no title line under it at all.
COMMISSIONER_LAYOUTS = {
    "title first": "Commissioner Delia Farnsworth",
    "colon": "Commissioner: Delia Farnsworth",
    "court commissioner": "Court Commissioner Delia Farnsworth",
    "title after a comma": "DELIA FARNSWORTH, Court Commissioner",
    "title on the next line": "Delia Farnsworth\nCourt Commissioner",
    "all caps block": "DELIA FARNSWORTH\nDISTRICT COURT COMMISSIONER",
    "district qualified": "Delia Farnsworth, Third District Court Commissioner",
    "heard before": "Heard before Commissioner Delia Farnsworth on March 4.",
    "recommendation, titled": ("RECOMMENDED BY THE COMMISSIONER:\n\n_______\n"
                               "Delia Farnsworth\nCourt Commissioner"),
    "recommendation, bare name": ("COMMISSIONER'S RECOMMENDATION:\n\n_______\n"
                                  "Delia Farnsworth"),
    "curly apostrophe": ("COURT COMMISSIONER’S RECOMMENDATION:\n\n_______\n"
                         "Delia Farnsworth"),
    "recommended by, signed": ("RECOMMENDED BY THE COURT COMMISSIONER:\n\n_______\n"
                               "/s/ Delia Farnsworth"),
}


@pytest.mark.parametrize("layout", sorted(COMMISSIONER_LAYOUTS))
def test_commissioners_are_harvested_in_every_layout(layout):
    found = officials.harvest(COMMISSIONER_LAYOUTS[layout])
    assert [o.name.casefold() for o in found] == ["delia farnsworth"]


@pytest.mark.parametrize("title,text", [
    ("Commissioner", "Commissioner Delia Farnsworth"),
    ("Hearing Officer", "Hearing Officer Delia Farnsworth"),
    ("Referee", "Referee Delia Farnsworth"),
    ("Magistrate Judge", "Magistrate Judge Delia Farnsworth"),
    ("Magistrate", "Magistrate Delia Farnsworth"),
    ("Chief Justice", "Chief Justice Delia Farnsworth"),
])
def test_every_officer_title_is_recognised_and_named(title, text):
    """"Magistrate Judge" doubles the title word; matching only the first half
    left "Judge Delia Farnsworth" as the candidate name, which reads as caption
    furniture and dropped the officer entirely."""
    found = officials.harvest(text)
    assert [o.name for o in found] == ["Delia Farnsworth"]
    assert found[0].title == title


@pytest.mark.parametrize("text", [
    "Commissioner Delia Farnsworth",
    "DELIA FARNSWORTH, Court Commissioner",
    "Hearing Officer Delia Farnsworth",
    "Referee Delia Farnsworth",
    "Magistrate Judge Delia Farnsworth",
])
def test_the_inline_allowlist_covers_the_same_titles(text):
    """The harvest is the belt; these patterns are the braces. Referee and
    hearing officer were in one and not the other."""
    covered = " ".join(text[s:e] for s, e in patterns.allowlist_spans(text))
    assert "farnsworth" in covered.casefold()


@pytest.mark.parametrize("text", [
    "The commissioner's recommendation was objected to.",
    "Commissioner of Insurance regulations apply here.",
    "Petitioner asked the commissioner to reconsider the ruling.",
])
def test_commissioner_in_ordinary_prose_harvests_nothing(text):
    assert officials.harvest(text) == []


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


# ------------------------------------- shapes found in real Utah pleadings --

# Every caption in the sample set prints the bench as a bare surname. Demanding
# two tokens left eight of eleven documents with no judicial protection at all.
SOLO_LAYOUTS = {
    "commissioner, colon": ("Commissioner: Blomquist", "Blomquist", "Commissioner"),
    "judge, colon": ("Judge: Kelly", "Kelly", "Judge"),
    "judge, space": ("Judge Brady", "Brady", "Judge"),
    "commissioner, space": ("Commissioner Ito", "Ito", "Commissioner"),
    "honorable": ("Hon. Blomquist", "Blomquist", "Judge"),
}


@pytest.mark.parametrize("layout", sorted(SOLO_LAYOUTS))
def test_a_bare_judicial_surname_is_harvested(layout):
    text, expected, title = SOLO_LAYOUTS[layout]
    found = officials.harvest(text)
    assert [(o.name, o.title) for o in found] == [(expected, title)]


def test_a_full_name_is_never_cut_back_to_its_first_word():
    """The solo pattern must not reduce "Judge Amber M. Cordova" to "Amber"."""
    found = officials.harvest("Judge Amber M. Cordova")
    assert [o.name for o in found] == ["Amber M. Cordova"]


def test_an_empty_title_line_harvests_nothing():
    """A real parentage decree carries "Commissioner: " with nothing after it."""
    assert officials.harvest("Commissioner: \nJudge: Samuel Chiara") == [
        officials.Official("Samuel Chiara", "Judge", "", "high")]


def test_a_solo_surname_still_answers_to_the_party_guard():
    """A judge called Kelly must not shield a child called Kelly."""
    bench = officials.harvest("Judge: Kelly")
    protection = officials.protected_terms(bench, avoid=["Kelly Anne Marchetti"])
    assert "Kelly" not in protection.terms


@pytest.mark.parametrize("prose", [
    "said Decree to be signed by the court and entered",
    "as ordered by the Court on March 4",
    "END OF DOCUMENT SIGNED BY THE COURT AS INDICATED BY THE ELECTRONIC SIGNATURE",
    "The parties are hereby awarded a Divorce Decree, said Decree to be signed by the court",
])
def test_by_the_court_inside_a_sentence_is_not_a_signing_block(prose):
    """Unanchored, this put both parties of a real divorce onto the bench."""
    assert not officials._BY_THE_COURT.search(prose)


def test_a_real_signing_heading_still_reads_as_one():
    for heading in ["BY THE COURT:", "  By the Court", "SO ORDERED.",
                    "COMMISSIONER'S RECOMMENDATION:", "___BY THE COURT:___"]:
        assert officials._BY_THE_COURT.search(heading), heading


def test_a_party_signature_block_is_not_a_judicial_officer():
    """A notary block sits under prose mentioning the court; both Hull parties
    were harvested onto the bench because of it."""
    text = ("That the parties are hereby awarded a Divorce Decree, said Decree "
            "to be signed by the court.\n"
            "Date _______Sign here ►____________________\n"
            "Marisol Ashdown\n"
            "I certify that Marisol Ashdown, who is known to me…\n")
    assert officials.harvest(text) == []


@pytest.mark.parametrize("line", [
    "West Jordan—Third District Court, 8080 S. Redwood Road, Suite 1701, West Jordan, UT 84088",
    "   Pleasant Grove, UT 84062",
    "225 South State, P.O. Box 1286, Roosevelt, Utah 84066",
    "721 West 1800 North",
])
def test_an_address_is_never_a_person_or_an_officer(line):
    """"West Jordan" was proposed as a party, ticked by default; "Pleasant
    Grove" reached the do-not-change list and would have shielded a real city
    from redaction."""
    from redactor import caption
    assert caption.is_address(line)
    assert caption._first_name_in(line) is None
    assert officials.harvest(f"Judge: {line}") == []


def test_a_genuine_name_is_not_mistaken_for_an_address():
    from redactor import caption
    assert not caption.is_address("Marisol Quenby Ashdown")
    assert caption._first_name_in("Marisol Quenby Ashdown") == "Marisol Quenby Ashdown"
