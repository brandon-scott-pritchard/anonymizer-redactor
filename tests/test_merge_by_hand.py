"""Merging one review row under another by hand: "Merge into..."."""

from redactor import grouping, merges, review


def _store():
    store, _ = review.build_store([("Abigail N Okafor", "person"), ("Abby Okafor", "person"),
                                   ("Tyson J Okafor", "person")])
    grouping.group(store)
    return store


def _row(store, text):
    return next(e for e in store.entities.values() if e.canonical == text)


def test_a_name_merged_under_a_main_name_takes_its_stand_in_in_its_own_shape():
    store = _store()
    abby, abigail = _row(store, "Abby Okafor"), _row(store, "Abigail N Okafor")
    assert grouping.head_of(store, abby) is not abigail
    assert merges.merge_by_hand(store, abby, abigail) == ""
    assert grouping.head_of(store, abby) is abigail
    assert abby.group_note == merges.MANUAL
    # the main name's invented surname, in the form's own two-word shape
    assert abby.replacement.split()[-1] == abigail.surrogate.last
    assert len(abby.replacement.split()) == 2


def test_a_merge_by_hand_stands_through_a_regrouping_and_split_undoes_it():
    store = _store()
    abby, abigail = _row(store, "Abby Okafor"), _row(store, "Abigail N Okafor")
    merges.merge_by_hand(store, abby, abigail)
    grouping.group(store)                       # starts from scratch
    merges.settle(store)
    assert grouping.head_of(store, abby) is abigail
    grouping.split(store, abby)
    merges.settle(store)
    assert grouping.head_of(store, abby) is abby
    assert abby.replacement != abigail.replacement


def test_an_address_merged_under_the_full_address_takes_its_placeholder():
    store = _store()
    short = store.add_value("street_address", "435 N 900 E")
    full = store.add_value("street_address", "4558 E Maple St Provo UT 84604")
    assert merges.merge_by_hand(store, short, full) == ""
    assert grouping.head_of(store, short) is full
    assert short.replacement == full.replacement


def test_rows_of_different_kinds_are_not_merged():
    store = _store()
    phone = store.add_value("phone", "(801) 555-0124")
    refused = merges.merge_by_hand(store, phone, _row(store, "Tyson J Okafor"))
    assert refused and grouping.head_of(store, phone) is phone


def test_a_merged_row_brings_its_own_forms_with_it():
    store = _store()
    a = store.add_value("street_address", "4558 E Maple St")
    b = store.add_value("street_address", "4558 East Maple Street")
    b.parent = a.key
    target = store.add_value("street_address", "514 W Center St Provo UT 84601")
    merges.merge_by_hand(store, a, target)
    assert grouping.head_of(store, b) is target and b.replacement == target.replacement


def test_a_party_row_merged_by_hand_and_split_back_is_the_partys_again():
    store = _store()
    row = store.add_value("street_address", "435 N 900 E APT 12")
    row.fixed, row.party, row.kind = True, "petitioner", "address"
    row.replacement = "[PETITIONER-ADDRESS-1]"
    target = store.add_value("street_address", "4558 E Maple St Provo UT 84604")
    merges.merge_by_hand(store, row, target)
    assert row.replacement == target.replacement
    grouping.split(store, row)
    merges.settle(store)
    assert (row.fixed, row.party, row.kind, row.replacement) == (
        True, "petitioner", "address", "[PETITIONER-ADDRESS-1]")
    assert grouping.head_of(store, row) is row
