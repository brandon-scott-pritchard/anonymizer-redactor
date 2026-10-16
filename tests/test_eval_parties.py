"""The party sheet the harness types from the labels, and the scores it is judged by.

Every name here is invented. A sheet that puts the wrong person in the
petitioner's place, or a score that counts a payee's surviving name as a
leak, would steer the parties-first work the wrong way without a sound.
"""

from pathlib import Path

import pytest

from eval import consistency, corpus_eval, sheet, truth
from redactor.mapping import Entity, MappingStore

HOME = {"line1": "1734 W GENTILE ST", "city": "LAYTON", "state": "UT", "zip": "84041",
        "zip4": "84041-4986"}

LABELS = [
    ("a/checking.json", {
        "layout": "D7", "institution": "Chase", "account_type": "checking",
        "holder": {"primary": "JANE E SMITH", "secondary": None, "address": HOME},
        "account_number": "541275624543",
        "data": {"txns": [
            {"kind": "p2p_out", "party": "EMERY BROWN", "net": "VENMO", "desc": "Venmo to Emery Brown"},
            {"kind": "card", "merchant": "COSTCO", "card": "6606"},
            {"kind": "transfer_out", "acct": "xxxxxx8731", "desc": "Transfer to Savings ..8731"},
            {"kind": "check", "check_no": 1136}]}}),
    ("a/savings.json", {
        "layout": "D4", "institution": "Pioneer", "account_type": "savings",
        "holder": {"primary": "JOHN Q SMITH", "secondary": None, "address": HOME},
        "account_number": "102798668731"}),
    ("b/zelle.json", {
        "layout": "Zelle", "holder": {"primary": "JANE E SMITH"},
        "data": {"txns": [
            {"type": "Sent", "name": "JOHN SMITH", "token": "(435) 555-0109", "conf": "NP2S2KTKK"},
            {"type": "Sent", "name": "Brynlee Thompson", "token": "(801) 555-0130"}]}}),
    ("c/eob.json", {"doc_type": "eob", "patient": "ZOE SMITH"}),
    ("d/registration.json", {"doc_type": "registration_renewal",
                             "vehicle": {"vin": "KEJ8ULCHH4E691WDA", "plate": "Y60 7PU",
                                         "owner": "JOHN Q SMITH"}}),
    ("e/pingo.json", {
        "layout": "P2", "holder": {"primary": "JANE E SMITH"},
        "data": {"txns": [{"id": "7119758592201276152", "frm": "Jane Smith", "to": "Bank ***4460"}],
                 "user": "@Jane-Smith-84"}}),
    ("f/note.json", {"doc_type": "promissory_note", "data": {"borrower": "Tyler J. Smith"}}),
    ("g/mortgage.json", {"layout": "M1", "account_type": "mortgage",
                         "holder": {"primary": "JANE E SMITH"}, "account_number": "779354811014",
                         "data": {"loan_no": "1319001670"}}),
    ("h/Form_1040.json", {"doc_type": "form_1040"}),
    ("i/orders.json", {"doc_type": "amazon_invoice", "holder": {"primary": "JANE E SMITH"},
                       "data": {"orders": [{"order_id": "112-7847741-7955330",
                                            "ship_to_name": "JANE E SMITH"}]}}),
]
RETURN = ("Your first name and middle initial / Last name\nJANE E  SMITH\n"
          "Your social security number\n943-75-6535\n"
          "If joint return, spouse's first name and middle initial / Last name\nJOHN Q  SMITH\n"
          "Spouse's social security number\n923-90-9016\nDependents\nZoe Smith, Max Smith\n")


def _case(typed_tails=False):
    labels = [(Path(p), label) for p, label in LABELS]
    texts = {Path("h/Form_1040.pdf"): RETURN}
    secrets, merchants = truth.Secrets(), set()
    for _p, label in labels:
        truth.collect(label, secrets, merchants)
    truth.collect_text(RETURN, secrets)
    truth.finalize(secrets, merchants)
    return sheet.build(labels, texts, secrets, typed_tails=typed_tails)


@pytest.fixture(scope="module")
def case():
    return _case()


def test_the_first_two_adults_are_the_parties_and_the_household_children_follow(case):
    roles = [(p.role, p.key, p.sheet["names"][0]) for p in case.parties]
    assert roles[:2] == [("petitioner", "petitioner", "JANE E SMITH"),
                         ("respondent", "respondent", "JOHN Q SMITH")]
    children = [(p.key, p.first) for p in case.parties if p.role == "child"]
    assert children == [("child-1", "ZOE"), ("child-2", "Max")]
    assert case.family_surnames == ["Smith"]
    # Jane written off a payment row is the petitioner, not a third party
    assert "Jane Smith" in case.party("petitioner").sheet["names"]


def test_a_brother_is_a_relative_and_a_payee_is_a_third_party(case):
    assert [p.first for p in case.relatives] == ["Tyler"]
    third = {p.full for p in case.third_people}
    assert {"EMERY BROWN", "Brynlee Thompson"} <= third
    assert not third & {"JANE E SMITH", "JOHN SMITH", "ZOE SMITH", "Tyler J. Smith"}


def test_every_party_has_the_sheet_shape(case):
    keys = set(sheet._blank_party("petitioner"))
    sheet_json = case.sheet()
    assert set(sheet_json) == {"parties", "family_surnames"}
    for party in sheet_json["parties"]:
        assert set(party) == keys
        assert party["role"] in sheet.ROLES


def test_identifiers_go_to_whom_the_labels_tie_them(case):
    jane, john = case.party("petitioner").sheet, case.party("respondent").sheet
    assert {"number": "541275624543", "institution": "Chase", "kind": "checking"} in jane["accounts"]
    assert {"number": "6606", "institution": "Chase"} in jane["cards"]
    assert [a["number"] for a in john["accounts"]] == ["102798668731"]
    # the SSN under each name on the return, not both on the filer
    assert jane["ssn"] == ["943-75-6535"] and john["ssn"] == ["923-90-9016"]
    # a Zelle token is the phone of the person on its row
    assert john["phones"] == ["(435) 555-0109"] and not jane["phones"]
    assert john["vehicles"] == [{"vin": "KEJ8ULCHH4E691WDA", "plate": "Y60 7PU"}]
    assert jane["handles"] == ["@Jane-Smith-84"]
    # the shared home once, under the statement holder seen first
    assert jane["addresses"] == ["1734 W GENTILE ST, LAYTON, UT 84041"]
    assert not john["addresses"]
    assert case.zip4 == {"84041-4986"}


def test_what_the_specification_leaves_as_written_is_kept_off_the_sheet(case):
    on_sheet = {a["number"] for p in case.parties for a in p.sheet["accounts"] + p.sheet["cards"]}
    assert not on_sheet & {"779354811014", "1319001670"}
    assert case.other_numbers["779354811014"] == "loan"
    assert case.other_numbers["1319001670"] == "loan"
    assert case.other_numbers["NP2S2KTKK"] == "confirmation"
    assert case.other_numbers["1136"] == "check"
    assert case.other_numbers["7119758592201276152"] == "transaction"
    assert case.third_values["phones"] == {"8015550130"}


def test_a_transfer_tail_of_a_known_account_is_that_account(case):
    tails = [a for a in case.accounts() if a["last4"] == "8731"]
    assert [a["number"] for a in tails] == ["102798668731"]


def test_a_tail_found_only_in_a_row_is_party_truth_but_not_typed(case):
    assert not any("4460" in a["number"] for p in case.parties for a in p.sheet["accounts"])
    assert case.found["petitioner"] == [{"number": "****4460", "institution": "", "kind": "linked"}]
    typed = _case(typed_tails=True)
    assert {"number": "****4460", "institution": "", "kind": "linked"} in \
        typed.party("petitioner").sheet["accounts"]


# --------------------------------------------------------------------------
# scores
# --------------------------------------------------------------------------


def test_party_leaks_count_the_family_and_not_the_payees(case):
    secrets = case.secrets()
    survived = ("Venmo to Emery Brown, Zelle to Brynlee Thompson (801) 555-0130 "
                "brynlee@example.com, Tyler repaid")
    assert truth.party_leaks(survived, secrets) == []
    assert {f.category for f in truth.party_leaks("paid to SMITH", secrets)} == {"household_name"}
    assert truth.party_leaks("Tyler Smith repaid", secrets)
    assert {f.category for f in truth.party_leaks("Zelle (435) 555-0109", secrets)} == {"phone"}
    assert {f.category for f in truth.party_leaks("SSN 923-90-9016", secrets)} >= {"ssn"}


@pytest.mark.parametrize("output, form", [
    ("Account ****4543", "masked"),
    ("Acct ending in 4543", "labelled tail"),
    ("Account 5412 7562 4543", "full"),
    ("Card 6606 Costco", "labelled tail"),
])
def test_a_party_account_left_showing_is_found_in_any_form(case, output, form):
    found = truth.account_forms_left(output, case.accounts())
    assert form in {f for a in found for f in a["forms"]}


def test_a_bare_four_digits_is_not_an_account_left_showing(case):
    assert truth.account_forms_left("Store #6606 Layton, 4543 units", case.accounts()) == []


def test_numbers_replaced_are_the_ones_that_are_not_the_parties(case):
    source = ("Order 112-7847741-7955330 Check #1136 Acct 541275624543 ending 4543\n"
              "Posted 2023-01-05 $1,234.56 call (801) 555-0130 LAYTON UT 84041 1734 W GENTILE ST\n")
    output = ("Order [ACCOUNT-1] Check #[ACCOUNT-2] Acct [PETITIONER-ACCOUNT-1] ending [PETITIONER-ACCOUNT-1]\n"
              "Posted 2023-01-05 $1,234.56 call (801) 555-0130 [PETITIONER-ADDRESS-1]\n")
    protected = corpus_eval._party_digits(case)
    accounts = [a["number"] for a in case.accounts() if len(a["number"]) >= 6]
    got = truth.numbers_replaced(source, output, protected, accounts, case.other_numbers,
                                 took=lambda digits: "bank_account")
    assert got["count"] == 2
    assert got["by_known"] == {"order": 1, "check": 1}
    assert got["by_took"] == {"bank_account": 2}


def test_a_phone_taken_as_an_account_is_mistyped(case):
    source = "Zelle to (801) 555-0130 and to 435-555-0109"
    output = "Zelle to [ACCOUNT-2] and to [RESPONDENT-PHONE-1]"
    kinds = {"8015550130": "bank_account", "4355550109": "phone"}
    got = truth.phones_mistyped(source, output, {"4355550109"}, took=kinds.get)
    assert (got["mistyped"], got["party_as_phone"], got["other_as_phone"]) == (1, 1, 0)
    assert got["examples"][0]["took"] == "bank_account"


def test_a_payee_renamed_is_a_third_party_replaced(case):
    source = "Venmo to Emery Brown\nZelle Brynlee Thompson (801) 555-0130\n1782 S Canyon Way"
    output = "Venmo to Alex Doe\nZelle Brynlee Thompson (801) 555-0130\n1782 S Canyon Way"
    got = truth.third_replaced(source, output, case.third_people,
                               {**case.third_values, "addresses": {"1782 S Canyon Way"}})
    assert (got["names"], got["phones"], got["addresses"]) == (1, 0, 0)


def _store(*rows):
    store = MappingStore()
    for category, canonical, replacement, parent in rows:
        key = canonical.casefold()
        store.entities[key] = Entity(key=key, category=category, canonical=canonical,
                                     replacement=replacement, parent=parent, occurrences=1)
    return store


def test_one_party_address_with_two_placeholders_is_a_split(case):
    store = _store(
        ("street_address", "1734 W GENTILE ST", "[PETITIONER-ADDRESS-1]", None),
        ("street_address", "1734 W Gentile St, Layton, UT 84041", "", "1734 w gentile st"),
        ("street_address", "1734 West Gentile Street", "[ADDRESS-3]", None),
        ("bank_account", "541275624543", "[PETITIONER-ACCOUNT-1]", None),
        ("masked_account", "****4543", "", "541275624543"),
    )
    scored = consistency.party_consistency(store, case)
    address = scored["petitioner"]["address"][0]
    assert address["stand_ins"] == 2
    account = scored["petitioner"]["account"][0]
    assert account["stand_ins"] == 1 and len(account["forms"]) == 2
    splits = consistency.party_splits(scored)
    assert splits == ["petitioner address 1734 W GENTILE ST, LAYTON, UT 84041: 2 stand-ins"]


def test_the_city_line_under_the_street_is_the_same_address(case):
    """"LAYTON UT 84041-4986" with a placeholder of its own is one house read
    as two - the split the attorney's overnight run was full of."""
    store = _store(("street_address", "1734 W GENTILE ST", "[ADDRESS-1]", None),
                   ("street_address", "LAYTON UT 84041-4986", "[ADDRESS-2]", None),
                   ("street_address", "84041-4986", "[ADDRESS-1]", "1734 w gentile st"),
                   ("street_address", "PROVO UT 84604", "[ADDRESS-3]", None))
    address = consistency.party_consistency(store, case)["petitioner"]["address"][0]
    assert address["stand_ins"] == 2
    assert not any("PROVO" in form for form in address["forms"])


def test_review_rows_are_the_heads_left_ticked_or_the_servers_groups():
    store = _store(("street_address", "1734 W GENTILE ST", "[A-1]", None),
                   ("street_address", "1734 W Gentile St, Layton", "", "1734 w gentile st"),
                   ("phone", "(801) 555-0130", "[PHONE-1]", None))
    store.entities["(801) 555-0130"].enabled = False
    assert consistency.review_rows(store) == {
        "rows": 1, "listed": 2, "found": 1, "by_kind": {"street_address": 1}, "source": "heads"}
    groups = [{"kind": "address"}, {"kind": "ssn"}, {"kind": "ssn"}]
    assert consistency.review_rows(store, groups)["rows"] == 3


def test_a_number_is_traced_to_the_entity_that_is_exactly_it():
    store = _store(("street_address", "1136 N Main St", "[A-1]", None),
                   ("check_number", "1136", "[CHECK-1]", None))
    took = consistency.taker(store)
    assert took("1136") == "check_number"
    assert took("999999") == ""


# --------------------------------------------------------------------------
# reaching the core
# --------------------------------------------------------------------------


def test_the_name_screen_pre_ticks_as_the_core_says_else_all_but_nicknames():
    assert corpus_eval._pre_ticked({"why": "nickname", "preticked": True})
    assert not corpus_eval._pre_ticked({"why": "glued", "preticked": False})
    assert corpus_eval._pre_ticked({"why": "OCR misreading"})
    assert not corpus_eval._pre_ticked({"why": "nickname"})


def test_the_sheet_is_one_the_core_accepts(case):
    """Where the parties-first core is present, the harness's sheet must pass
    its validation as ``PUT /api/parties`` would - or every --parties batch
    fails before it is scanned."""
    core = corpus_eval.parties_core()
    if core is None:
        pytest.skip("redactor.parties is not in this checkout")
    model = core.PartySheet.from_json(case.sheet())
    assert [p.role for p in model.parties][:2] == ["petitioner", "respondent"]
