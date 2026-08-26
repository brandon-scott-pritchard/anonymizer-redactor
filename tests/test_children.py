"""Children, and the names a caption hands over uncapitalised.

Across a real sample of divorce, paternity and parentage decrees, not one child
was ever proposed: they are never in the caption, and the caption is the only
place the name screen used to look. A minor's full name and date of birth sat
in the delivered document unless the operator noticed and typed them in.

The shapes are real; the names are invented.
"""

import pytest

from redactor import caption, children, engine, pipeline, review
from redactor.engine import Settings

ROSTER_TABLE = [
    ["Name", "Month & Year of Birth"],
    ["JQA", "04/2020"],
    ["CEA", "06/2016"],
    ["LRA", "01/2007"],
]

CUE_PROSE = """Children. The parentage petition concerned the following child.
The name, birth month, and birth year of the minor child is listed below.
Marisol Shai Ashdown Born: December 2017
"""


# --------------------------------------------------------------- harvesting --

def test_a_roster_table_yields_every_child():
    found = children.from_tables(ROSTER_TABLE and [ROSTER_TABLE])
    assert [c.name for c in found] == ["JQA", "CEA", "LRA"]
    assert [c.born for c in found] == ["04/2020", "06/2016", "01/2007"]


def test_a_cue_line_and_a_born_date_yield_a_child():
    found = children.from_text(CUE_PROSE)
    assert [(c.name, c.born) for c in found] == [("Marisol Shai Ashdown", "December 2017")]


def test_a_child_written_with_a_slash_date_is_found():
    found = children.from_text(
        "Children.  Rowena is the legal mother and Rylan is the legal father.\n"
        "a. Duke Ashdown Born 07/15/2018\n")
    assert [(c.name, c.born) for c in found] == [("Duke Ashdown", "07/15/2018")]


def test_the_bare_word_children_is_not_a_cue():
    """It appears six to ten times per document as ordinary prose."""
    prose = ("The parties are unaware of any protective order cases involving the "
             "parties' children.\nMarisol Ashdown Born: December 2017\n")
    assert children.from_text(prose) == []


def test_a_table_without_a_birth_column_is_not_a_roster():
    assert children.from_tables([[["Asset", "Identifier"],
                                  ["2019 Toyota", "VIN 5TDJZRFH8KS998172"]]]) == []


def test_an_emancipated_child_is_flagged_rather_than_typed_a_minor():
    """"LRA legally emancipated on January 26, 2025 when she turned 18." """
    note = ("LRA legally emancipated on January 26, 2025 when she turned 18. "
            "All other children of the parties are minors.")
    found = {c.name: c for c in children.from_tables([ROSTER_TABLE], note)}
    assert found["LRA"].emancipated
    assert found["LRA"].role == "Child (now an adult)"
    assert not found["JQA"].emancipated
    assert found["JQA"].role == "Minor child"


def test_initials_and_full_names_are_typed_differently():
    initials, full = children.Child("JQA"), children.Child("Marisol Ashdown")
    assert initials.is_initials and initials.category == "minor_initials"
    assert not full.is_initials and full.category == "minor"


# --------------------------------------------------------- through the store --

def test_a_child_roster_redacts_initials_and_dates(tmp_path):
    """The operator's decision: both the initials and the birth dates go."""
    entries = [("JQA", "minor_initials"), ("04/2020", "dob")]
    store, _ = review.build_store(entries)
    settings = Settings(use_ner=False)
    text = "JQA 04/2020 shall reside with the primary parent."
    out = engine.apply_hits(text, engine.scan_text(text, store, settings))
    assert "JQA" not in out and "04/2020" not in out
    assert "[CHILD-" in out and "[DOB-" in out


def test_three_letter_initials_are_not_dropped_by_the_length_floor():
    """MIN_LITERAL_LENGTH silently discarded every child on a roster."""
    store, _ = review.build_store([("JQA", "minor_initials")])
    matcher = engine.EntityMatcher(store, Settings(use_ner=False))
    matcher._refresh_values()
    assert matcher._value_rules, "a three-character child must still be matched"


def test_a_son_named_after_his_father_keeps_his_own_identity():
    """A paternity decree registered both as ONE person, silently.

    "Marcus Ashdown" and "Marcus Shai Ashdown" look exactly like one name
    written two ways; only the roster says otherwise.
    """
    store, _ = review.build_store(
        [("Alejandro Ashdown", "person"), ("Alejandro Shai Ashdown", "person")],
        roles={"alejandro shai ashdown": "Minor child"})
    assert len(store.entities) == 2, "father and son must not share a pseudonym"
    replacements = {e.replacement for e in store.entities.values()}
    assert len(replacements) == 2


def test_an_ordinary_pair_of_written_forms_still_merges():
    store, _ = review.build_store(
        [("John Ashdown", "person"), ("John Michael Ashdown", "person")])
    assert len(store.entities) == 1


# ---------------------------------------------- uncapitalised caption parties --

@pytest.mark.parametrize("block,expected", [
    ("marisol quenby ashdown,\nRespondent.\n", "Marisol Quenby Ashdown"),
    ("Rylan MacLeod ashdown,\nPetitioner,\n", "Rylan MacLeod Ashdown"),
    ("MARISOL ASHDOWN,\nPetitioner,\n", "MARISOL ASHDOWN"),
])
def test_a_hand_typed_caption_party_is_harvested_and_tidied(block, expected):
    """A live petition names its respondent in lower case; requiring an initial
    capital meant that party was never proposed at all."""
    found = caption.harvest(block)
    assert [c.name for c in found] == [expected]


def test_title_casing_leaves_internal_capitals_and_particles_alone():
    assert caption._titlecase("rylan macLeod ashdown") == "Rylan MacLeod Ashdown"
    assert caption._titlecase("marisol van der berg") == "Marisol van der Berg"
    assert caption._titlecase("MARISOL ASHDOWN") == "MARISOL ASHDOWN"


def test_lowercase_prose_is_not_swept_up_as_a_party():
    """The lenient token is only admitted where a role word vouches for it."""
    assert caption.harvest("the parties agree that custody shall be shared\n") == []


# ------------------------------- children named without a date, and aliases --

CUSTODY = ("CHILD CUSTODY. The parties shall share joint legal and split physical "
           "custody of the minor children. Petitioner shall be the primary custodial "
           "parent of Theo and Rory Ashdown. Respondent shall be the primary "
           "custodial parent of Emory Ashdown.\n"
           "TAX DEDUCTIONS FOR DEPENDENT CHILDREN. Petitioner shall claim Rory each "
           "year for tax purposes; Respondent shall claim Emory.")


def test_children_named_only_in_a_custody_clause_are_found():
    """A decree can name every child and never print a date of birth.

    The roster and cue-line harvesters both need one, so all three of these
    children were invisible and shipped intact.
    """
    found = {c.name for c in children.from_custody_clauses(CUSTODY)}
    assert {"Theo", "Rory Ashdown", "Emory Ashdown", "Rory", "Emory"} <= found


def test_a_custody_clause_does_not_run_on_into_the_sentence():
    """re.IGNORECASE made the capitalised-token rule meaningless, and the match
    swallowed the following words: "Rory each", "Theo in"."""
    found = {c.name for c in children.from_custody_clauses(CUSTODY)}
    assert not any(" each" in name or " in" in name for name in found)


def test_the_short_and_long_form_of_a_child_fold_together():
    entries = [(c.name, "minor") for c in children.from_custody_clauses(CUSTODY)]
    store, overlaps = review.build_store(entries)
    canonicals = {e.canonical for e in store.entities.values()}
    assert "Emory" not in canonicals, "the bare first name folds into the full one"
    assert overlaps


@pytest.mark.parametrize("text,pair", [
    ("Marisol Ashdown shall return to her former name of Rowena Radcliffe.",
     ("Marisol Ashdown", "Rowena Radcliffe")),
    ("Marisol Ashdown, also known as Marisol Quenby, testified.",
     ("Marisol Ashdown", "Marisol Quenby")),
    ("Marisol Ashdown, f/k/a Marisol Quenby", ("Marisol Ashdown", "Marisol Quenby")),
])
def test_a_restored_or_former_name_is_paired_with_the_current_one(text, pair):
    """A decree restoring a maiden name prints both, and both identify her."""
    assert caption.former_names(text) == [pair]


def test_the_former_name_pattern_does_not_swallow_the_verb():
    """Under a blanket IGNORECASE this returned "Marisol Ashdown Shall"."""
    current, _former = caption.former_names(
        "Marisol Ashdown shall return to her former name of Rowena Radcliffe.")[0]
    assert current == "Marisol Ashdown"


def test_ordinary_prose_yields_no_former_name():
    assert caption.former_names("The parties shall return to mediation.") == []
