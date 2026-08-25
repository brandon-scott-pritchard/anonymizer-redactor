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
    assert "Commissioner Russell Minas" in covered


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
