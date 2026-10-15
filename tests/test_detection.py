"""Detectors, the allowlist, and name-variant expansion."""

import pytest

from redactor import caption, names, ner, patterns, surrogates
from redactor.mapping import MappingStore, read_encrypted_key, write_encrypted_key


def categories_found(text):
    protected = patterns.allowlist_spans(text)
    return {m.category for m in patterns.scan(text, protected=protected)}


@pytest.mark.parametrize("category", [
    "ssn", "email", "phone", "fax", "street_address", "bank_account", "routing_number",
    "credit_card", "vin", "license_plate", "ein", "payment_handle", "passport", "mrn",
    "policy_number", "parcel_number", "deed_reference", "legal_description",
    "case_number", "dob", "investment_account",
])
def test_each_category_is_detected(sample_text, category):
    assert category in categories_found(sample_text)


def test_statutes_rules_and_citations_are_protected(sample_text):
    protected = patterns.allowlist_spans(sample_text)
    covered = " ".join(sample_text[s:e] for s, e in protected)
    for phrase in ("Utah Code Ann. Section 30-3-5", "Rule 26", "Jones v. Jones, 2019 UT App 12"):
        assert phrase in covered


@pytest.mark.parametrize("text,expected", [
    ("Utah Code Ann. Section 30-3-5(1)(a)", "Section 30-3-5(1)(a)"),
    ("42 U.S.C. Sec. 1983", "Sec. 1983"),
    ("Utah Code Ann. Sec 30-3-5", "Sec 30-3-5"),
    ("Under Section 78B-12-202", "Section 78B-12-202"),
])
def test_section_references_survive_in_every_written_form(text, expected):
    protected = patterns.allowlist_spans(text)
    covered = " ".join(text[s:e] for s, e in protected)
    assert expected in covered


def test_judicial_officers_are_never_touched(sample_text):
    protected = patterns.allowlist_spans(sample_text)
    covered = " ".join(sample_text[s:e] for s, e in protected)
    assert "Judge Amber M. Cordova" in covered
    assert "Commissioner Delia Farnsworth" in covered


def test_a_bare_number_that_fails_luhn_is_not_a_card():
    """Unlabeled digit runs must clear the checksum before we touch them."""
    assert "credit_card" not in categories_found("The sequence 4111 1111 1111 1112 appeared.")
    assert "credit_card" in categories_found("The sequence 4111 1111 1111 1111 appeared.")


def test_a_labeled_card_is_redacted_even_if_the_checksum_fails():
    """A typo in a card number does not make it safe to publish."""
    assert "credit_card" in categories_found("Card number 4111 1111 1111 1112")


def test_a_label_word_is_never_taken_as_the_value():
    found = patterns.scan("Her Venmo handle is @jsmith-slc")
    assert [m.text for m in found if m.category == "payment_handle"] == ["@jsmith-slc"]


def test_ordinary_prose_produces_nothing():
    prose = ("The parties met in the spring and separated the following year. "
             "Counsel appeared and the matter was taken under advisement.")
    assert categories_found(prose) == set()


# ------------------------------------------------------------------- names --


def test_name_expands_into_every_written_form():
    parsed = names.parse("John Michael Smith")
    forms = {v.text for v in names.variants(parsed)}
    for expected in ("John Michael Smith", "John Smith", "Smith, John", "J. Smith",
                     "John M. Smith", "Mr. Smith", "Smith", "John", "Michael", "Smiths"):
        assert expected in forms


def test_comma_form_and_particles_parse():
    assert names.parse("Smith, Jane E.").canonical == "Jane E. Smith"
    assert names.parse("Maria van der Berg").last == "van der Berg"


@pytest.mark.parametrize("text,should_match", [
    ("Smith", True), ("Smith's", True), ("Smiths", False), ("Smithers", False),
    ("Blacksmith", False),
])
def test_word_boundaries_hold(text, should_match):
    assert bool(names.variant_regex("Smith").search(text)) is should_match


def test_surrogates_are_deterministic():
    first = surrogates.person("John Michael Smith", want_middle=True)
    second = surrogates.person("John Michael Smith", want_middle=True)
    assert first.canonical == second.canonical
    assert first.canonical != "John Michael Smith"


def test_a_family_shares_one_surrogate_surname():
    store = MappingStore()
    petitioner = store.add_person("Jane Elizabeth Smith")
    respondent = store.add_person("John Michael Smith")
    child = store.add_person("Tommy Smith", category="minor")
    surnames = {e.surrogate.last for e in (petitioner, respondent, child)}
    assert len(surnames) == 1
    firsts = {e.surrogate.first for e in (petitioner, respondent, child)}
    assert len(firsts) == 3


def test_two_people_never_share_a_full_surrogate_name():
    store = MappingStore()
    made = {store.add_person(f"Person Number{i} Smith").replacement for i in range(25)}
    assert len(made) == 25


# ----------------------------------------------------------------- caption --


def test_caption_yields_parties_with_their_roles(sample_text):
    found = {c.name: c.role for c in caption.harvest(caption.caption_region(sample_text))}
    assert found.get("JANE ELIZABETH SMITH") == "Petitioner"
    assert found.get("JOHN MICHAEL SMITH") == "Respondent"


def test_caption_reader_ignores_court_and_title_furniture(sample_text):
    names_found = {c.name for c in caption.harvest(caption.caption_region(sample_text))}
    for furniture in ("THIRD JUDICIAL DISTRICT", "SALT LAKE COUNTY", "STATE OF UTAH"):
        assert furniture not in names_found


def test_in_re_captions_are_read():
    text = "IN THE MATTER OF THE MARRIAGE OF\nROBERTA CHEN-ALVAREZ and DAVID PAUL ALVAREZ"
    found = {c.name for c in caption.harvest(caption.caption_region(text))}
    assert "ROBERTA CHEN-ALVAREZ" in found
    assert "DAVID PAUL ALVAREZ" in found


# ------------------------------------------------------------ mapping key --


def test_mapping_key_round_trips(tmp_path):
    store = MappingStore()
    store.add_person("Jane Elizabeth Smith", role="Petitioner")
    store.add_value("ssn", "528-41-9963")
    path = write_encrypted_key(store, tmp_path / "key.json", "correct horse")
    recovered = read_encrypted_key(path, "correct horse")
    originals = {row["original"] for row in recovered["entities"]}
    assert "528-41-9963" in originals


def test_mapping_key_rejects_the_wrong_password(tmp_path):
    store = MappingStore()
    store.add_value("ssn", "528-41-9963")
    path = write_encrypted_key(store, tmp_path / "key.json", "right")
    with pytest.raises(Exception):
        read_encrypted_key(path, "wrong")


def test_the_key_file_never_contains_plaintext(tmp_path):
    store = MappingStore()
    store.add_value("ssn", "528-41-9963")
    path = write_encrypted_key(store, tmp_path / "key.json", "pw")
    assert "528-41-9963" not in path.read_text()


# ---------------------------------------------------------- NER refinement --

@pytest.mark.parametrize("value", [
    "JANE ELLEN SMITH",          # ALL-CAPS caption form
    "John Smith",
    "Maria de la Cruz",          # particles
    "Robert J. Smith Jr.",       # initial and suffix
])
def test_person_shaped_org_suggestions_become_people(value):
    assert ner.refine_category(value, "organization") == "person"


@pytest.mark.parametrize("value", [
    "Smith & Associates",
    "Zions Bank",
    "Wasatch Elementary School",
    "Salt Lake County",
    "Granite Credit Union",
    "Smith Family Trust",
])
def test_real_organizations_keep_their_category(value):
    assert ner.refine_category(value, "organization") == "organization"


def test_refine_leaves_other_categories_alone():
    assert ner.refine_category("John Smith", "person") == "person"
    assert ner.refine_category("Unit 4B Building 7", "location") == "location"


def test_norp_is_not_mapped_to_organization():
    assert "NORP" not in ner.LABEL_MAP


# ------------------------------------------------------------- prefilters --

# Adversarial texts aimed at every prefilter: digit-free values, dotted label
# variants, keyword-free prose. Turning the prefilters off must never change
# what scan() or allowlist_spans() returns on any of them.
PREFILTER_PROBES = [
    "Ordinary prose about the family home, the marriage, and the children.",
    "The parties agree that the marital estate shall be divided equitably.",
    "Recorded in Book Seven, at Pages Twelve through Fourteen.",
    "Lot A, Block B, Willow Creek Subdivision.",
    "Device address AB:CD:EF:AB:CD:EF was seen on the network.",
    "SWIFT: ABCDEF GH and the account SWIFT/BIC CDEFABZZ.",
    "Her PayPal handle is @jane-pays; the Venmo handle is @jsmith-slc.",
    "Petitioner was born in Salt Lake City, Utah.",
    "See Jones v. Jones and Smith vs. Wesson for the standard.",
    "Clerk of the Court, Third Judicial District, Judge Amber M. Cordova.",
    "S.S.N. 528-41-9963 and D.O.B. 04/17/1985 appear on page one.",
    "The d.l. is X-244-9921 and the A.P.N. 22-14-377-009.",
    "Tel. 8015550184, fax: 801-555-0199, license plate no. DXKQAA.",
    "Certificate of insurance no. CI-994412 covers the residence.",
    "Their 401(k) no. RT-99183772 and the plain account no. 000148829371.",
    "An e-filed document 22FA1234 with envelope no. 8812.",
    "Section 30-3-5 and Rule 26 and 42 U.S.C. § 1983 are cited.",
]


def test_prefilters_change_no_output(sample_text, monkeypatch):
    for text in [sample_text, *PREFILTER_PROBES]:
        fast_spans = patterns.allowlist_spans(text)
        fast = patterns.scan(text, protected=fast_spans)
        monkeypatch.setattr(patterns, "PREFILTER", False)
        slow_spans = patterns.allowlist_spans(text)
        slow = patterns.scan(text, protected=slow_spans)
        monkeypatch.setattr(patterns, "PREFILTER", True)
        assert fast_spans == slow_spans, f"allowlist differs on: {text!r}"
        assert fast == slow, f"scan differs on: {text!r}"


# ------------------------------------------------- scan-fix regressions --
# Each of these encodes a leak confirmed during the full-repo review.

from redactor import engine as _engine
from redactor.engine import Settings as _Settings


def test_an_identifier_starting_with_r_is_not_shielded():
    """The rules-of-procedure allowlist must not degenerate to bare R+digits."""
    assert "drivers_license" in categories_found(
        "Driver's License No. R12345678 was suspended.")


@pytest.mark.parametrize("text", [
    "Rule 26", "Rules of Civil Procedure 12", "Fed. R. Civ. P. 12(b)",
    "Utah R. Civ. P. 26(b)",
])
def test_rule_citations_are_still_protected(text):
    assert patterns.allowlist_spans(text), text


@pytest.mark.parametrize("text,category", [
    ("SSN: XXX-XX-6789", "ssn"),
    ("SSN ***-**-6789", "ssn"),
    ("Card No. XXXX-XXXX-XXXX-1234", "credit_card"),
    ("debit card ending in 9876", "credit_card"),
    ("DOB: 1990-01-05", "dob"),
    ("Facsimile: 801-555-0199", "fax"),
    ("device at fe80::1 connected", "ip_address"),
    ("Smith v Jones settled the question.", "case_name"),
])
def test_previously_leaking_forms_are_caught(text, category):
    assert category in categories_found(text)


def test_typographic_apostrophes_match_typed_names():
    store = MappingStore()
    store.add_person("Sean O'Brien")
    out, hits = _engine.scan_and_apply(
        "Mr. O’Brien and Sean O’Brien appeared.", store, _Settings(), "doc")
    assert "O’Brien" not in out and "O'Brien" not in out
    assert hits


def test_a_surrogate_never_equals_a_real_persons_name():
    store = MappingStore()
    first = store.add_person("John Smith")
    fake = first.replacement
    late_party = store.add_person(fake)       # a real person named exactly that
    assert first.replacement.casefold() != late_party.canonical.casefold()
    assert first.surrogate.last.casefold() != late_party.person.last.casefold()


def test_a_party_surnamed_ward_keeps_their_name():
    assert caption.plausible_name("John Ward")
    text = "JOHN WARD,\n    Petitioner,\nv.\nJANE WARD,\n    Respondent."
    found = {c.name for c in caption.harvest(text)}
    assert "JOHN WARD" in found and "JANE WARD" in found


def test_title_and_plural_forms_survive_single_token_off():
    store = MappingStore()
    store.add_person("John Smith")
    settings = _Settings(include_single_token_names=False)
    out, _hits = _engine.scan_and_apply(
        "Mr. Smith and the Smiths met.", store, settings, "doc")
    assert "Mr. Smith" not in out and "the Smiths" not in out


# ----------------------------------------------- one person, one pseudonym --

def test_short_and_full_forms_are_one_person():
    store = MappingStore()
    short = store.add_person("John Smith")
    full = store.add_person("John Michael Smith")
    assert short is full
    assert full.canonical == "John Michael Smith"
    assert len([e for e in store.entities.values() if e.is_person]) == 1


def test_the_richer_form_wins_regardless_of_order():
    store = MappingStore()
    full = store.add_person("John Michael Smith")
    short = store.add_person("John Smith")
    assert short is full
    assert full.canonical == "John Michael Smith"


def test_an_initial_matches_the_written_out_form():
    store = MappingStore()
    a = store.add_person("J. Smith")
    b = store.add_person("John Smith")
    assert a is b and b.canonical == "John Smith"


def test_different_people_never_merge():
    store = MappingStore()
    jane = store.add_person("Jane Smith")
    john = store.add_person("John Smith")
    senior = store.add_person("Robert Smith Sr.")
    junior = store.add_person("Robert Smith Jr.")
    assert jane is not john
    assert senior is not junior


def test_merged_person_matches_both_written_forms():
    store = MappingStore()
    store.add_person("John Smith")
    store.add_person("John Michael Smith")
    out, _ = _engine.scan_and_apply(
        "John Smith signed. John Michael Smith agreed.", store, _Settings(), "doc")
    assert "Smith" not in out
    fake = next(e for e in store.entities.values() if e.is_person).surrogate
    assert out.count(fake.last) == 2, "both forms must get the same pseudonym"


# ------------------------------------------------------------- typo forms --

@pytest.mark.parametrize("typo", [
    "Johhn Smith",       # doubled letter
    "Johnn Smith",       # extra letter at a boundary
    "John Smiith",       # epenthesis in the surname
    "Jonh Smith",        # adjacent transposition
    "John Smiht",        # adjacent transposition in the surname
])
def test_typo_forms_of_a_registered_name_are_caught(typo):
    store = MappingStore()
    store.add_person("John Smith")
    out, hits = _engine.scan_and_apply(f"{typo} appeared today.", store,
                                       _Settings(), "doc")
    assert hits, typo
    for token in typo.split():
        assert token not in out, f"{token} leaked from {typo!r}"


def test_short_words_are_not_typo_matched():
    store = MappingStore()
    store.add_person("Al Ray")        # both under the fuzzy length floor
    out, _ = _engine.scan_and_apply("All of the era.", store, _Settings(), "doc")
    assert "All" in out and "era" in out


# ------------------------- proposal quality, from a real fifty-row list --

@pytest.mark.parametrize("place", [
    "Pleasant Grove", "Cedar Valley", "Spring Creek", "Oak Ridge", "Elk Hollow",
])
def test_a_place_the_model_labelled_a_place_is_not_promoted_to_a_person(place):
    """The reverse leak. A town passes every person test - two capitalised
    words, no digits, no company marker - so it was promoted to a person and
    would have been given an invented human name while the real place shipped.

    Keyed on generic geographic words rather than on the cities that happened
    to be in the sample, half of which are surnames: a party called Jordan,
    Logan or Murray stopped being proposed at all when it was tried that way.
    """
    from redactor import ner
    assert ner.refine_category(place, "location") == "location"


def test_a_bare_person_label_on_a_town_is_a_known_limit():
    """When the model says PERSON and gives no corroborating signal, nothing
    short of a gazetteer separates "Pleasant Grove" from "Sarah Park". It stays
    a person - still redacted, just under the wrong heading - and the operator
    retypes it on the review screen. Guessing here would cost every real
    Park, Glen and Bay on a name list."""
    from redactor import ner
    assert ner.refine_category("Twin Falls", "person") == "person"
    assert ner.refine_category("Sarah Park", "person") == "person"


def test_an_explicit_marker_still_wins_over_the_place_check():
    from redactor import ner
    assert ner.refine_category("Salt Lake County", "organization") == "organization"


@pytest.mark.parametrize("value", [
    "Cascade Pediatric Dentistry", "Wasatch Valley Pediatrics", "Cascade Orthodontics",
])
def test_a_medical_practice_is_an_organization_not_a_person(value):
    """These came back as people, so each would have been given a human name."""
    from redactor import ner
    assert ner.refine_category(value, "person") == "organization"


@pytest.mark.parametrize("junk", [
    "Order", "Debts", "Titles", "Time", "Vaccines", "Childcare", "Custody",
])
def test_an_ordinary_word_is_not_a_person(junk):
    """One real decree offered "Order" eight times, as a PERSON. Fifty rows of
    this is an unreviewable list, and an unreviewable list gets skipped."""
    from redactor import ner
    assert not ner._plausible(junk, "person")


@pytest.mark.parametrize("junk", [
    "Venmo Account", "PayPal Account Mother", "Marcus Vaughn Address",
    "Print Name", "Mother's Day",
])
def test_a_label_run_together_with_something_else_is_not_a_person(junk):
    from redactor import ner
    assert not ner._plausible(junk, "person")


def test_a_run_of_names_on_separate_lines_is_not_one_person():
    """A supervised-contact list arrived as a single candidate; ticking it
    registered a three-person entity that matched none of them."""
    from redactor import ner
    assert not ner._plausible("Petra Vandermeer Rachel Bly Steven Cole", "person")


@pytest.mark.parametrize("real", [
    "Marlow Cole", "Paul Bly", "Marisol Quenby Ashdown", "Marcuse Vaughn",
])
def test_the_noise_filters_do_not_eat_real_names(real):
    from redactor import ner
    assert ner._plausible(real, "person")


def test_a_nickname_is_linked_to_the_party_it_belongs_to():
    """"Chrissy" was offered as an ORGANIZATION while Christine sat on the name
    list, and the nickname shipped in both files."""
    from redactor import names as _names
    assert _names.nickname_for("Chrissy", ["Christine Annell Vaughn"]) == "Christine Annell Vaughn"
    assert _names.nickname_for("Jake", ["Jacob Duane Vaughn"]) == "Jacob Duane Vaughn"


def test_a_nickname_two_parties_could_claim_is_left_alone():
    from redactor import names as _names
    assert _names.nickname_for("Chrissy", ["Christine Vaughn", "Christine Adams"]) is None


def test_the_same_party_reaching_the_check_twice_is_not_an_ambiguity():
    """The store and the caption list both offer her, and counting them twice
    read as two Christines."""
    from redactor import names as _names
    twice = ["Christine Annell Vaughn", "Christine Annell Vaughn"]
    assert _names.nickname_for("Chrissy", twice) == "Christine Annell Vaughn"


def test_an_ordinary_word_is_not_a_nickname():
    from redactor import names as _names
    assert _names.nickname_for("Marisol", ["Christine Vaughn"]) is None


def test_a_name_that_is_an_everyday_word_is_matched_only_with_its_capital():
    """Ticked as people, "Hope Baker" and "The Home Depot" made every "hope",
    "baker" and "the" in a household's statements a stand-in - 29% of the
    ordinary words on its typed pages."""
    store = MappingStore()
    store.add_person("Hope Baker")
    store.add_person("The Home Depot")
    out, _hits = _engine.scan_and_apply(
        "I hope the baker baked. Hope Baker signed. BAKER, HOPE. The statement.",
        store, _Settings(), "doc")
    assert out.startswith("I hope the baker baked. ")
    assert "Hope Baker" not in out and "BAKER" not in out
    assert out.endswith("The statement.")


@pytest.mark.parametrize("line,category", [
    ("01/24 82633072107526 Summit Home #1177 Layton $96.00", "check_number"),
    ("01/09 01/07 24974938269185115 RED ROBIN 7413 LAYTON 59.83", "check_number"),
    ("Card number 4111 1111 1111 1111", "credit_card"),
    ("***-**-6789", "ssn"),
    ("XXX-XX-6789 dependent", "ssn"),
])
def test_a_rows_reference_is_not_a_card_and_a_masked_ssn_is_an_ssn(line, category):
    from redactor import patterns
    assert [m.category for m in patterns.scan(line)] == [category]


def test_a_year_like_tail_is_never_searched_for_and_a_date_is_never_an_account():
    """A tail of "2021" registered as an account took the year out of 1,234
    dates across one household's documents."""
    from redactor import patterns
    from redactor.engine import Settings, apply_hits, scan_text

    for value, category in [("2021-03-30", "bank_account"), ("July1", "bank_account"),
                            ("period08/01/2021-07/31/2022", "policy_number")]:
        assert patterns._wrong_shape(category, value)
    store = MappingStore()
    store.add_value("masked_account", "2021")
    store.add_value("credit_card", "4417")
    text = "Statement 08/31/2021 card ending 4417; paid 2021 taxes"
    out = apply_hits(text, scan_text(text, store, Settings(use_ner=False)))
    assert "08/31/2021" in out and "paid 2021 taxes" in out and "4417" not in out


@pytest.mark.parametrize("text,kept", [
    ("Zelle toABIGAILOKAFOR Conf 12", "Zelle to"),
    ("fromWhitneyLewisCont487333789 $40.00", "Cont487333789 $40.00"),
    ("toNOAHOKAFORConf", "Conf"),
    ("NATHANCBLACKBURN", ""),
])
def test_a_name_run_together_with_a_payment_line_is_the_person(text, kept):
    """"toABIGAILOKAFOR" read as a payment handle and got a bracket, where the
    same person was an invented name everywhere else."""
    from redactor import grouping, review
    from redactor.engine import Settings, apply_hits, scan_text

    store, _ = review.build_store([("NOAH OKAFOR", "person"), ("ABIGAIL N OKAFOR", "person"),
                                   ("Whitney Lewis", "person"), ("NATHAN C BLACKBURN", "person")])
    grouping.group(store)
    out = apply_hits(text, scan_text(text, store, Settings(use_ner=False, pdf_mode="anonymize")))
    for real in ("ABIGAIL", "OKAFOR", "NOAH", "Whitney", "Lewis", "BLACKBURN"):
        assert real.casefold() not in out.casefold()
    assert kept in out and "[" not in out


@pytest.mark.parametrize("text", ["Annual Market report", "Smithfield Annex", "Conference Center"])
def test_a_word_holding_a_name_and_something_else_is_left_alone(text):
    from redactor import grouping, review
    from redactor.engine import Settings, apply_hits, scan_text

    store, _ = review.build_store([("Ann Lee", "person"), ("Mark Hill", "person"),
                                   ("William Smith", "person"), ("NATHAN C BLACKBURN", "person")])
    grouping.group(store)
    assert apply_hits(text, scan_text(text, store, Settings(use_ner=False))) == text


def test_a_name_inside_an_email_goes_with_the_whole_address():
    """The run-together pass swapped the name in "abigail.okafor98@example.com"
    and left the address, digits and domain, where it had been [EMAIL-1]."""
    from redactor import grouping, review
    from redactor.engine import Settings, apply_hits, scan_text

    store, _ = review.build_store([("ABIGAIL N OKAFOR", "person")])
    grouping.group(store)
    out = apply_hits("Abigail Okafor abigail.okafor98@example.com",
                     scan_text("Abigail Okafor abigail.okafor98@example.com", store,
                               Settings(use_ner=False, pdf_mode="anonymize")))
    assert "@" not in out and "98" not in out and "[EMAIL-" in out


@pytest.mark.parametrize("value", ["12/21/2023", "12/21/23", "02/20", "01 2024 01", "2024-03-30",
                                   "30.03.2024", "03/2024", "May 31, 2021", "31-Jan-24", "Jan-24",
                                   "July1", "period08/01/2021-07/31/2022", "Dated 3/5/24"])
def test_a_date_in_any_common_form_is_never_an_account(value):
    """One household lost 852 dates to "12/21/2023", "02/20" and "01 2024 01"
    registered as bank and investment accounts, then replaced everywhere."""
    from redactor import patterns
    assert patterns.looks_like_date(value)
    for category in ("bank_account", "investment_account", "check_number", "policy_number"):
        assert patterns._wrong_shape(category, value)
    assert not patterns._wrong_shape("dob", value)


@pytest.mark.parametrize("value", ["3-6815", "2-5666", "12-34-5678", "123-45-6789", "4417220193",
                                   "8102 4477 9301", "621-555-0143", "1974 S Aspen Way"])
def test_numbers_that_are_not_dates_stay_numbers(value):
    from redactor import patterns
    assert not patterns.looks_like_date(value)


def test_nothing_but_a_birth_date_is_taken_from_inside_a_date():
    from redactor import review
    from redactor.engine import Settings, apply_hits, scan_text

    store, _ = review.build_store([("May Thompson", "person")])
    store.add_value("bank_account", "12/21/2023")         # as a detector once did
    text = "Paid May 31, 2021 to May Thompson on 12/21/2023. DOB: 12/06/1986"
    out = apply_hits(text, scan_text(text, store, Settings(use_ner=False, pdf_mode="anonymize")))
    assert "May 31, 2021" in out and "12/21/2023" in out
    assert "Thompson" not in out and "12/06/1986" not in out
