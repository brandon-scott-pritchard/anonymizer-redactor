"""One row per person, address or number, however many ways it was written.

The review list showed "BRETT H" beside "BRETT H. PRITCHARD", and "123 N Main
St" beside "123 North Main Street", as unrelated rows with unrelated
replacements. These pin the grouping: what joins, what is kept apart, and that
a tick on the row - or on one form inside it - means what it says.
"""

import pytest

from redactor import engine, grouping, review
from redactor.engine import Settings
from redactor.mapping import MappingStore

ANON = Settings(use_ner=False, docx_mode="anonymize")


def _store(people=(), values=()):
    store, _ = review.build_store([(name, "person") for name in people])
    for category, text, *count in values:
        entity = store.add_value(category, text)
        entity.occurrences = count[0] if count else 1
    grouping.group(store)
    return store


def _row(store, text):
    return next(e for e in store.entities.values() if e.canonical == text)


def _head(store, text):
    return grouping.head_of(store, _row(store, text)).canonical


def _apply(store, text):
    return engine.apply_hits(text, engine.scan_text(text, store, ANON))


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------


def test_a_partial_name_joins_the_full_name_it_is_part_of():
    store = _store(["BRETT H. PRITCHARD", "BRETT H", "PRITCHARD BRETT"])
    assert _head(store, "BRETT H") == "BRETT H. PRITCHARD"
    assert _head(store, "PRITCHARD BRETT") == "BRETT H. PRITCHARD"


def test_a_partial_reads_the_same_as_the_full_names_own_form_of_it():
    """The row and the running text agree: "Brett H" is the stand-in's first
    name and middle, written the way the full name's stand-in writes them."""
    store = _store(["BRETT H. PRITCHARD", "BRETT H"])
    head = _row(store, "BRETT H. PRITCHARD")
    first, middle = head.surrogate.first, head.surrogate.middles[0]
    assert _row(store, "BRETT H").replacement == f"{first} {middle}"
    assert _apply(store, "Brett H signed.") == f"{first} {middle} signed."


def test_a_partial_that_fits_two_people_stays_on_its_own_row():
    store = _store(["BRETT H. PRITCHARD", "Jane Pritchard", "PRITCHARD"])
    alone = _row(store, "PRITCHARD")
    assert alone.parent is None
    assert set(alone.candidates) == {"BRETT H. PRITCHARD", "Jane Pritchard"}
    assert "Could be" in review.review_note(alone)


def test_two_initials_are_not_a_partial_name():
    from redactor import names
    assert grouping._partial_mapping(names.parse("B. H."),
                                     names.parse("BRETT H. PRITCHARD")) is None
    assert grouping._partial_mapping(names.parse("Brett H"),
                                     names.parse("BRETT H. PRITCHARD")) is not None


def test_a_child_named_like_a_parent_is_not_folded_into_the_parent():
    store = _store(["DANIEL R HOLBROOK", "MASON HOLBROOK"])
    assert _row(store, "MASON HOLBROOK").parent is None


def test_an_unticked_form_is_left_as_written():
    """Unticking "BRETT H" inside the group means that form stays, though the
    full name's own variants include it."""
    store = _store(["BRETT H. PRITCHARD", "BRETT H"])
    _row(store, "BRETT H").enabled = False
    out = _apply(store, "Brett H. Pritchard and BRETT H")
    assert out.endswith("and BRETT H")
    assert "Pritchard" not in out


def test_a_shared_surname_goes_while_either_person_is_ticked():
    """Unticking the ambiguous "PRITCHARD" row cannot keep Jane's surname: the
    same letters are hers, and she is still ticked. The full name goes whole."""
    store = _store(["BRETT H. PRITCHARD", "Jane Pritchard", "PRITCHARD"])
    _row(store, "PRITCHARD").enabled = False
    out = _apply(store, "Brett H. Pritchard wrote; Pritchard agreed.")
    assert "Pritchard" not in out and "Brett" not in out


def test_an_initial_matches_with_or_without_its_period():
    store = _store(["Brett H. Pritchard"])
    assert "Pritchard" not in _apply(store, "Brett H Pritchard paid")
    assert "Brett" not in _apply(store, "BRETT H paid")


# ---------------------------------------------------------------------------
# addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", [
    "123 North Main Street", "123 N. MAIN ST.", "123 N Mian St",
    "123 Main St", "123 N Main St, Salt Lake City, UT 84105-1385",
])
def test_an_address_written_another_way_joins_it(variant):
    store = _store(values=[("street_address", "123 N Main St", 5),
                           ("street_address", variant)])
    assert _head(store, variant) == "123 N Main St"
    assert _row(store, variant).replacement == _row(store, "123 N Main St").replacement


def test_the_most_frequent_form_heads_the_group():
    store = _store(values=[("street_address", "123 N Main St", 2),
                           ("street_address", "123 North Main Street", 9)])
    assert _head(store, "123 N Main St") == "123 North Main Street"


@pytest.mark.parametrize("other", ["132 N Main St", "124 N Main St"])
def test_a_different_house_number_is_a_different_address(other):
    store = _store(values=[("street_address", "123 N Main St", 3),
                           ("street_address", other)])
    assert _row(store, other).parent is None
    assert _row(store, other).replacement != _row(store, "123 N Main St").replacement


def test_a_transposed_house_number_is_flagged_not_merged():
    store = _store(values=[("street_address", "123 N Main St", 3),
                           ("street_address", "132 N Main St")])
    assert "different house number" in review.review_note(_row(store, "132 N Main St"))


def test_a_different_unit_or_zip_is_a_different_address():
    store = _store(values=[("street_address", "900 E 400 S APT 4", 3),
                           ("street_address", "900 East 400 South Apt 5"),
                           ("street_address", "123 N Main St, Provo UT 84604", 2),
                           ("street_address", "123 N Main St, Ogden UT 84403")])
    assert _row(store, "900 East 400 South Apt 5").parent is None
    assert _row(store, "123 N Main St, Ogden UT 84403").parent is None


def test_a_form_with_no_unit_joins_the_one_unit_it_could_be():
    store = _store(values=[("street_address", "900 E 400 S APT 4", 3),
                           ("street_address", "900 East 400 South")])
    assert _head(store, "900 East 400 South") == "900 E 400 S APT 4"
    store = _store(values=[("street_address", "900 E 400 S APT 4", 3),
                           ("street_address", "900 E 400 S APT 5", 2),
                           ("street_address", "900 East 400 South")])
    assert _row(store, "900 East 400 South").parent is None
    assert _row(store, "900 East 400 South").candidates


def test_the_city_line_and_zip_plus_four_join_their_street():
    store = _store(values=[("street_address", "1734 W GENTILE ST", 4),
                           ("street_address", "1734 W GENTILE ST LAYTON UT 84041", 2),
                           ("street_address", "LAYTON UT 84041"),
                           ("street_address", "84041-4986")])
    assert _head(store, "LAYTON UT 84041") == "1734 W GENTILE ST"
    assert _head(store, "84041-4986") == "1734 W GENTILE ST"


def test_a_value_first_met_during_the_run_joins_its_group():
    store = _store(values=[("street_address", "123 N Main St", 3)])
    store.grouped = True
    late = store.add_value("street_address", "123 North Main Street")
    assert late.parent == _row(store, "123 N Main St").key
    assert late.replacement == _row(store, "123 N Main St").replacement


# ---------------------------------------------------------------------------
# companies, accounts, phones, emails
# ---------------------------------------------------------------------------


def test_a_company_however_its_suffix_and_spelling_are_written():
    store = _store(values=[("organization", "Silicon Slopes Analytics, Inc.", 3),
                           ("organization", "SILICON SLOPES ANALYTICS INC"),
                           ("employer", "Silicon Slops Analytics")])
    assert _head(store, "SILICON SLOPES ANALYTICS INC") == "Silicon Slopes Analytics, Inc."
    assert _head(store, "Silicon Slops Analytics") == "Silicon Slopes Analytics, Inc."
    out = _apply(store, "Paid by Silicon Slopes Analytics LLC on 3/1")
    assert "Silicon" not in out and "on 3/1" in out


def test_an_account_and_its_masked_tail_are_one_row():
    store = _store(values=[("bank_account", "3357 3537 2072", 3),
                           ("bank_account", "335735372072"),
                           ("masked_account", "XXXXXXXX2072"),
                           ("credit_card", "2072")])
    for form in ("335735372072", "XXXXXXXX2072", "2072"):
        assert _head(store, form) == "3357 3537 2072"


def test_a_tail_two_accounts_share_is_kept_on_its_own():
    store = _store(values=[("bank_account", "335735372072", 3),
                           ("bank_account", "998811112072", 2),
                           ("masked_account", "XXXX2072")])
    tail = _row(store, "XXXX2072")
    assert tail.parent is None and len(tail.candidates) == 2


def test_a_phone_however_it_is_punctuated():
    store = _store(values=[("phone", "(801) 555-0165", 3), ("phone", "801-555-0165"),
                           ("phone", "+1 801 555 0165"), ("phone", "(801) 555-0166")])
    assert _head(store, "801-555-0165") == "(801) 555-0165"
    assert _head(store, "+1 801 555 0165") == "(801) 555-0165"
    assert _row(store, "(801) 555-0166").parent is None


# ---------------------------------------------------------------------------
# ticking, splitting, rescanning
# ---------------------------------------------------------------------------


def test_ticking_a_group_ticks_every_form_and_a_form_alone_only_itself():
    store = _store(values=[("street_address", "123 N Main St", 3),
                           ("street_address", "123 North Main Street")])
    head, form = _row(store, "123 N Main St"), _row(store, "123 North Main Street")
    grouping.set_enabled(store, head, False)
    assert not form.enabled
    grouping.set_enabled(store, form, True)
    assert form.enabled and not head.enabled


def test_a_split_form_gets_its_own_replacement_and_stays_split_after_a_rescan():
    values = [("street_address", "123 N Main St", 3), ("street_address", "123 North Main Street")]
    store = _store(values=values)
    form = _row(store, "123 North Main Street")
    grouping.split(store, form)
    assert form.parent is None
    assert form.replacement != _row(store, "123 N Main St").replacement
    decided = review.snapshot_decisions(store)
    fresh = _store(values=values)
    review.carry_decisions(fresh, decided)
    assert _row(fresh, "123 North Main Street").parent is None


def test_editing_a_heads_replacement_carries_to_its_forms():
    store = _store(values=[("street_address", "123 N Main St", 3),
                           ("street_address", "123 North Main Street")])
    review.set_replacement(_row(store, "123 N Main St"), "[HOME]", store)
    assert _row(store, "123 North Main Street").replacement == "[HOME]"


def test_the_mapping_key_names_the_group_each_form_belongs_to():
    store = _store(values=[("street_address", "123 N Main St", 3),
                           ("street_address", "123 North Main Street")])
    rows = {r["original"]: r for r in store.rows()}
    assert rows["123 North Main Street"]["same_as"] == "123 N Main St"


def test_the_review_list_says_what_a_tick_does():
    assert "Ticked = this WILL BE CHANGED" in review.REVIEW_INSTRUCTIONS
    assert "Unticked = left exactly as written" in review.REVIEW_INSTRUCTIONS


def test_prescan_groups_what_it_found(tmp_path):
    docx = pytest.importorskip("docx")
    from redactor import docx_processor, pipeline
    path = tmp_path / "letter.docx"
    document = docx.Document()
    for line in ["Send it to 1734 W Gentile St, Layton UT 84041.",
                 "Earlier mail went to 1734 West Gentile Street.",
                 "Card ending in 4417. Account 3357 3537 4417."]:
        document.add_paragraph(line)
    document.save(path)
    store = MappingStore()
    pipeline.prescan([path], store, ANON)
    street = next(e for e in store.entities.values()
                  if e.canonical.startswith("1734 West"))
    assert street.parent is not None
    out = tmp_path / "out.docx"
    docx_processor.process(path, out, store, ANON)
    text = "\n".join(docx_processor.extract_text(out).values())
    assert "Gentile" not in text and "4417" not in text
    # both spellings of the street became one placeholder
    head = grouping.head_of(store, street)
    assert text.count(head.replacement) >= 2


def test_a_form_with_no_town_cannot_chain_two_towns_together():
    store = _store(values=[("street_address", "2750 Aspen Springs Dr, Park City UT 84060", 9),
                           ("street_address", "2750 ASPEN SPRINGS DR"),
                           ("street_address", "2750 Aspen Springs Dr, Twin Falls ID 83301")])
    assert _row(store, "2750 Aspen Springs Dr, Twin Falls ID 83301").parent is None
    loose = _row(store, "2750 ASPEN SPRINGS DR")
    assert loose.parent is None and len(loose.candidates) == 2


def test_a_street_mid_line_does_not_take_the_next_lines_town():
    """A row reading of an order export: the billing street ends one line and
    the shipping city opens the next, in another column."""
    from redactor import patterns
    text = "Bath Set   2750 ASPEN SPRINGS DR\nTWIN FALLS ID 83301   PARK CITY UT 84060"
    assert [m.text for m in patterns.scan(text, enabled=["street_address"])
            if "ASPEN" in m.text] == ["2750 ASPEN SPRINGS DR"]
    block = "JONATHAN WHITFIELD\n2750 Aspen Springs Dr\nPark City, UT 84060-8757"
    assert "2750 Aspen Springs Dr\nPark City, UT 84060-8757" in [
        m.text for m in patterns.scan(block, enabled=["street_address"])]


# ---------------------------------------------------------------------------
# from the review of the grouping
# ---------------------------------------------------------------------------


def test_a_son_named_after_his_father_is_never_folded_into_him():
    """Registered apart on purpose - a child roster - and then re-merged by
    the grouping, father and son shared one pseudonym."""
    store, _ = review.build_store([("Daniel R Holbrook", "person"), ("Daniel Holbrook", "person")],
                                  roles={"daniel holbrook": "child"})
    grouping.group(store)
    son, father = _row(store, "Daniel Holbrook"), _row(store, "Daniel R Holbrook")
    assert son.parent is None and son.replacement != father.replacement


def _scanned(extra=()):
    store, _ = review.build_store([])
    for text, count in [("123 N Main St", 3), ("123 North Main Street", 1), *extra]:
        store.add_value("street_address", text).occurrences = count
    grouping.group(store)
    store.grouped = True
    return store


def test_a_split_row_never_shares_its_old_heads_placeholder_after_a_rescan():
    store = _scanned()
    grouping.split(store, _row(store, "123 North Main Street"))
    decided = review.snapshot_decisions(store)
    fresh = _scanned(extra=[("9 Elm St", 1)])          # numbering shifts by one
    review.carry_decisions(fresh, decided)
    split, head = _row(fresh, "123 North Main Street"), _row(fresh, "123 N Main St")
    assert split.parent is None and split.replacement != head.replacement


def test_no_two_rows_ever_share_a_replacement():
    store = _scanned(extra=[("9 Elm St", 1), ("77 Oak Ave", 2)])
    heads = [e.replacement for e in store.entities.values() if e.parent is None]
    assert len(heads) == len(set(heads))


def test_east_and_west_on_a_grid_are_two_addresses():
    """A form that dropped its direction chained "620 E 1200 S" to
    "620 W 1200 S" - two houses, one placeholder."""
    store = _store(values=[("street_address", "620 E 1200 S", 5),
                           ("street_address", "620 W 1200 S", 4),
                           ("street_address", "620 1200 S", 1)])
    east, west, loose = (_row(store, t) for t in ("620 E 1200 S", "620 W 1200 S", "620 1200 S"))
    assert east.replacement != west.replacement
    assert loose.parent is None and len(loose.candidates) == 2
    store = _store(values=[("street_address", "620 E 1200 S", 5),
                           ("street_address", "620 1200 S", 1)])
    assert _head(store, "620 1200 S") == "620 E 1200 S"


def test_a_town_with_no_zip_is_the_same_town():
    store = _store(values=[("street_address", "123 N Main St, Salt Lake City, UT 84105", 3),
                           ("street_address", "123 N Main St, Salt Lake City, UT")])
    assert _head(store, "123 N Main St, Salt Lake City, UT") == "123 N Main St, Salt Lake City, UT 84105"


def test_a_five_digit_po_box_is_a_box_not_a_zip():
    assert grouping.parse_address("PO Box 12345").box == "12345"


def test_one_word_companies_a_letter_apart_are_two_companies():
    store = _store(values=[("organization", "Vans", 2), ("organization", "Vons", 1)])
    assert _row(store, "Vons").parent is None


def test_retyping_a_head_onto_its_twin_keeps_its_forms_listed():
    store = _store(values=[("organization", "Silicon Slopes Analytics, Inc.", 3),
                           ("organization", "SILICON SLOPES ANALYTICS INC", 1),
                           ("employer", "Silicon Slopes Analytics, Inc.", 1)])
    store.grouped = True
    head = next(e for e in store.entities.values()
                if e.category == "organization" and e.canonical == "Silicon Slopes Analytics, Inc.")
    review.retype_entity(store, head, "employer")
    assert all(e.parent is None or e.parent in store.entities for e in store.entities.values())
    assert _row(store, "SILICON SLOPES ANALYTICS INC").parent in store.entities


def test_an_edited_replacement_follows_its_group_when_the_head_changes():
    store = _scanned()
    review.set_replacement(_row(store, "123 N Main St"), "[HOME]", store)
    decided = review.snapshot_decisions(store)
    fresh = _scanned(extra=[("123 North Main Street", 9)])   # the other form now leads
    fresh.entities["street_address:123 north main street"].occurrences = 9
    grouping.group(fresh)
    review.carry_decisions(fresh, decided)
    assert {e.replacement for e in fresh.entities.values() if "123" in e.canonical} == {"[HOME]"}


def test_a_form_met_mid_run_stays_ticked_under_an_unticked_group():
    store = _scanned()
    grouping.set_enabled(store, _row(store, "123 N Main St"), False)
    late = store.add_value("street_address", "123 N. Main Street")
    assert late.parent and late.enabled
