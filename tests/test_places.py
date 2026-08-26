"""The gazetteer confirms places; it never proposes them and never gates names."""

import gzip

import pytest

from redactor import caption, engine, mapping, pipeline, places


# ---------------------------------------------------------------------------
# the shipped data
# ---------------------------------------------------------------------------


def test_the_gazetteer_ships_and_loads():
    table = places.gazetteer()
    assert len(table) > 90_000, "the data file looks truncated"
    assert places.known("Provo") and places.known("SALT LAKE CITY")
    assert not places.known("Zzyzxville")


def test_the_data_file_carries_its_provenance():
    with gzip.open(places.DATA, "rt", encoding="utf-8") as fh:
        header = "".join(line for line in fh if line.startswith("#"))
    assert "Census" in header and "GNIS" in header
    assert "public domain" in header


def test_canonical_spelling_comes_from_the_gazetteer_not_from_title_casing():
    """A statement header shouts; the name list should not repeat the shouting."""
    found = places.harvest("ACCOUNT HOLDER\nMCKINNEY TX 75070")
    assert [p.name for p in found] == ["McKinney"], "expected McKinney, not Mckinney"


# ---------------------------------------------------------------------------
# both halves are required: context proposes, the gazetteer confirms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("Respondent resides at 1234 Maple Lane\nSouth Jordan, UT 84095.", "South Jordan"),
    ("BIG SKY FEDERAL CREDIT UNION\n900 MAIN ST\nWEST JORDAN UT 84088", "West Jordan"),
    ("Petitioner now resides in Provo, Utah.", "Provo"),
    ("The parties moved to Roosevelt, UT.", "Roosevelt"),
    ("Mother resides in Pleasant Grove.", "Pleasant Grove"),
    ("Father is a resident of Coeur d'Alene.", "Coeur d'Alene"),
    ("The parties married in Truth or Consequences, NM 87901.", "Truth or Consequences"),
    ("Employer is located in Isle of Palms, South Carolina.", "Isle of Palms"),
])
def test_context_plus_gazetteer_finds_the_town(text, expected):
    assert [p.name for p in places.harvest(text)] == [expected]


@pytest.mark.parametrize("text", [
    # context fires, the gazetteer refuses - every one of these was a real
    # false positive from the context patterns running unconfirmed
    "Pursuant to the Uniform Interstate Family Support Act, jurisdiction lies here.",
    "The court applies the Enforcement Act, and the Uniform Parentage Act.",
    "Petitioner resides in Whartlebury, UT 84000.",
])
def test_context_alone_proposes_nothing(text):
    assert places.harvest(text) == []


@pytest.mark.parametrize("text", [
    # the gazetteer would match, no context pattern fires. Rule, Telephone,
    # Holiday and Golden are all real US towns, and so are the tool's own
    # invented surrogate names.
    "See Rule 26, Utah R. Civ. P.",
    "Telephone: 801-555-0134",
    "The Chesterton Trust holds the property in Zephyr and Underhill.",
    "Holiday parent-time is governed by the schedule; Golden hours apply.",
    "Salt Lake County is the proper venue.",
])
def test_the_gazetteer_alone_proposes_nothing(text):
    assert places.harvest(text) == []


# ---------------------------------------------------------------------------
# the guards
# ---------------------------------------------------------------------------


def test_a_month_after_born_in_is_a_date_not_a_town():
    """May, June, March and August are all populated places."""
    assert places.harvest("The child was born in May 2019.") == []
    assert places.harvest("Their second child was born in August.") == []


def test_a_credential_is_not_a_state():
    """MD is Maryland and also a doctor; Miller is a surname and also a town."""
    assert places.harvest("Jane Miller, MD examined the child.") == []
    assert places.harvest("Report of Thomas Hull, PA, dated June 1.") == []
    # the same shape with a ZIP behind it is an address, and does fire
    assert [p.name for p in places.harvest("Miller, MO 65707")] == ["Miller"]


def test_a_street_name_is_trimmed_off_the_front_of_the_city():
    """The run is greedy; the city is the part against the comma."""
    found = places.harvest("1234 Maple Lane South Jordan, UT 84095")
    assert [p.name for p in found] == ["South Jordan"]


def test_the_residence_cue_anchors_on_the_left():
    """"resides in Provo in Utah County" is Provo, not Utah."""
    found = places.harvest("Respondent resides in Provo in Utah County.")
    assert [p.name for p in found] == ["Provo"]


def test_a_run_never_crosses_a_line_break():
    """A two-line address block fused the street's "N." onto the city.

        17775 W. 7000 N.
        Altonah, Utah  84001

    gave a run of "N. Altonah", and the single-word guard threw it away.
    """
    text = "Mindy Morris\n17775 W. 7000 N.\nAltonah, Utah  84001\n"
    assert [p.name for p in places.harvest(text)] == ["Altonah"]


def test_the_venue_recital_is_not_an_address():
    """"residents of Salt Lake County, Utah" is not the town of Salt Lake."""
    text = "Petitioner and the minor child were residents of Salt Lake County, Utah."
    assert places.harvest(text) == []


def test_a_county_in_a_separate_clause_does_not_veto_the_town():
    """The county word only counts directly after the match."""
    found = places.harvest("Respondent resides in Provo in Utah County.")
    assert [p.name for p in found] == ["Provo"]


def test_an_allowlist_span_vetoes_the_hit():
    text = "Petitioner resides in Provo, Utah."
    start = text.index("Provo")
    assert places.harvest(text) != []
    assert places.harvest(text, protected=((start, start + 5),)) == []


# ---------------------------------------------------------------------------
# the surname collision - 5,478 US place names are also surnames
# ---------------------------------------------------------------------------


def test_a_party_whose_surname_is_a_town_vetoes_the_town():
    text = "Respondent Jesse Hull resides in Hull, IA 51239."
    assert [p.name for p in places.harvest(text)] == ["Hull"]
    assert places.harvest(text, avoid=frozenset({"jesse hull"})) == []


def test_the_veto_matches_a_single_token_of_a_longer_name():
    text = "The parties lived in Fowler, CO 81039."
    assert places.harvest(text, avoid=frozenset({"christine fowler"})) == []


def test_the_veto_does_not_swallow_a_longer_place_name():
    """A party named Jordan must not shield the town of South Jordan."""
    text = "Respondent resides in South Jordan, UT 84095."
    found = places.harvest(text, avoid=frozenset({"robert jordan", "jordan"}))
    assert [p.name for p in found] == ["South Jordan"]


def test_the_gazetteer_never_gates_name_harvesting():
    """The regression this whole design exists to avoid.

    An earlier attempt put Utah city names into caption.BOILERPLATE so that
    addresses would stop being read as parties. The side effect was that eight
    real party names - every one of which is also a US town - stopped being
    proposed at all. Names and places are decided independently, and this test
    fails the moment they are coupled again.
    """
    text = (
        "IN THE FOURTH JUDICIAL DISTRICT COURT, UTAH COUNTY, STATE OF UTAH\n\n"
        "ROBERT JORDAN,\n        Petitioner,\nv.\n"
        "SARAH MURRAY,\n        Respondent.\n"
    )
    proposed = {item.name.casefold() for item in caption.harvest(text)}
    assert "robert jordan" in proposed
    assert "sarah murray" in proposed


# ---------------------------------------------------------------------------
# through the pipeline
# ---------------------------------------------------------------------------


def test_places_are_proposed_even_with_the_model_switched_off(sample_docx):
    """Locations come from rules, so they survive a run with no spaCy."""
    store = mapping.MappingStore()
    settings = engine.Settings(use_ner=False)
    found, notes = pipeline.collect_suggestions([sample_docx], store, settings)
    names = {s.text for s in found}
    assert "Sandy" in names, f"expected the town from the address block, got {names}"
    assert all(s.category == "location" for s in found)
    assert any("turned off" in n for n in notes)


def test_a_ticked_party_is_never_proposed_as_a_place(sample_docx):
    """Sandy is a town in Utah and also a given name."""
    store = mapping.MappingStore()
    settings = engine.Settings(use_ner=False)
    party = caption.CaptionName("Sandy", "Petitioner", "x", "high", "person")
    found, _ = pipeline.collect_suggestions([sample_docx], store, settings,
                                            caption_names=[party])
    assert "Sandy" not in {s.text for s in found}
