"""The party sheet: who the case is about, and every way their details are printed.

Every name, number and address here is invented.
"""

import pickle
import queue

import pytest

from redactor import audit, engine, grouping, parties, patterns, pipeline
from redactor.mapping import MappingStore

SHEET = {"parties": [
    {"role": "petitioner", "names": ["Jane Elizabeth Smith", "Jane E. Doe"],
     "dob": ["1986-12-06"], "ssn": ["528-41-9963"],
     "addresses": ["1734 W Gentile St, Layton, UT 84041"],
     "phones": ["(801) 555-0143"], "emails": ["jane.smith@example.com"],
     "businesses": ["Smith Landscaping LLC"],
     "accounts": [{"number": "****1234", "institution": "Chase", "kind": "checking"}],
     "handles": ["@janesmith86"]},
    {"role": "respondent", "names": ["John Michael Smith"], "ssn": ["531-22-7781"],
     "phones": ["801-555-0199"], "employers": ["Red Canyon Lands"],
     "accounts": [{"number": "000123456789", "institution": "Zions"}],
     "case_numbers": ["224900871"]},
    {"role": "child", "names": ["Emma Rose Smith"], "dob": ["03/21/2014"]},
]}


def make_store(sheet=SHEET):
    store = MappingStore()
    parties.register(parties.PartySheet.from_json(sheet), store)
    grouping.group(store)
    store.grouped = True
    return store


def run(text, store=None, page=0):
    store = store or make_store()
    settings = engine.Settings(parties_first=True)
    token = parties.PAGE.set(page)
    try:
        hits = engine.scan_text(text, store, settings, engine.EntityMatcher(store, settings))
    finally:
        parties.PAGE.reset(token)
    return engine.apply_hits(text, hits), store


def head(store, owner, kind):
    return next(e for e in store.entities.values()
                if e.fixed and e.parent is None and e.party == owner and e.kind == kind)


# ---------------------------------------------------------------- the sheet --

def test_the_sheet_is_normalised_and_the_family_surname_derived():
    sheet = parties.PartySheet.from_json(SHEET)
    assert sheet.family_surnames == ["Smith"]
    petitioner = sheet.parties[0]
    assert petitioner.phones == ["(801) 555-0143"]
    assert petitioner.dob == ["1986-12-06"]
    assert sheet.parties[2].dob == ["2014-03-21"]
    assert [p.owner for p in sheet.parties] == ["petitioner", "respondent", "child-1"]
    shown = sheet.to_json()["parties"][2]["dob"][0]
    assert shown == {"value": "03/21/2014", "canonical": "2014-03-21"}
    # sent back as it was shown, it reads the same
    again = parties.PartySheet.from_json(sheet.to_json())
    assert again.parties[2].dob == ["2014-03-21"]
    assert again.parties[0].accounts[0]["number"] == "****1234"


@pytest.mark.parametrize("field,value,message", [
    ("ssn", "12-34", "9 digits"),
    ("dob", "not a date", "a date"),
    ("phones", "555-0143", "10 digits"),
])
def test_a_bad_value_names_its_party_and_field(field, value, message):
    with pytest.raises(parties.PartyError) as caught:
        parties.PartySheet.from_json({"parties": [{"role": "respondent", field: [value]}]})
    detail = caught.value.detail()
    assert detail["party"] == 0 and detail["role"] == "respondent"
    assert detail["field"] == field
    assert message in detail["message"] and detail["message"].startswith("Respondent")


def test_an_unknown_role_is_refused():
    with pytest.raises(parties.PartyError) as caught:
        parties.PartySheet.from_json({"parties": [{"role": "uncle"}]})
    assert caught.value.field == "role"


# -------------------------------------------------------- printed forms --

def test_ssn_forms():
    forms = parties.ssn_forms("528-41-9963")
    for form in ("528-41-9963", "528 41 9963", "528419963", "***-**-9963", "XXX-XX-9963",
                 "*****9963"):
        assert form in forms


def test_phone_forms():
    forms = parties.phone_forms("(801) 555-0143")
    for form in ("(801) 555-0143", "(801)555-0143", "801-555-0143", "801.555.0143",
                 "801 555 0143", "8015550143", "+1 801 555 0143", "1-801-555-0143",
                 "+18015550143"):
        assert form in forms


def test_dob_forms_are_all_that_date_and_no_other():
    forms = parties.dob_forms("1986-12-06")
    for form in ("12/06/1986", "12/6/1986", "12-06-86", "1986-12-06", "December 6, 1986",
                 "Dec 6, 1986", "6 Dec 1986", "06DEC1986"):
        assert form in forms
    assert all(patterns.looks_like_date(f) for f in forms)
    assert all(parties.parse_date(f).isoformat() == "1986-12-06" for f in forms
               if parties.parse_date(f) is not None)
    # read day-first, "06/12/1986" is somebody else's birthday in June
    assert "06/12/1986" not in forms


def test_account_forms_cover_every_mask_of_the_last_four():
    forms = parties.account_forms("000123456789")
    for form in ("000123456789", "0001 2345 6789", "****6789", "XXXX6789", "...6789",
                 "XXXX-XXXX-XXXX-6789", "x6789"):
        assert form in forms


def test_other_identifier_forms():
    assert "@janesmith86" in parties.handle_forms("@janesmith86")
    assert "janesmith86" in parties.handle_forms("@janesmith86")
    assert "224900871" in parties.id_forms("22-4900871")
    assert "22 4900871" in parties.id_forms("22-4900871")


def test_a_bare_street_folds_into_the_full_address_and_another_door_does_not():
    store = make_store({"parties": [{"role": "petitioner", "names": ["Jane Smith"],
                                     "addresses": ["1734 W Gentile St Apt 4, Layton, UT 84041"]}]})
    home = head(store, "petitioner", "address")
    assert parties.fold(store, "address", "1734 West Gentile Street") is home
    assert parties.fold(store, "address", "1734 W GENTILE ST APT 4\nLAYTON UT 84041") is home
    assert parties.fold(store, "address", "1734 Gentile St, Layton, UT 84041") is home
    assert parties.fold(store, "address", "1736 W Gentile St") is None
    assert parties.fold(store, "address", "1734 W Gentile St Apt 9") is None


def test_the_city_line_is_the_partys_only_under_their_street():
    text = "Mail to:\n1734 W Gentile St\nLAYTON UT 84041\n\nThe Layton office: LAYTON UT 84041\n"
    out, _ = run(text, page=3)
    assert "1734" not in out
    assert out.count("LAYTON UT 84041") == 1


# --------------------------------------------------------- role labels --

def test_role_labels_and_one_row_per_party_value():
    store = make_store()
    assert head(store, "petitioner", "ssn").replacement == "[PETITIONER-SSN]"
    assert head(store, "respondent", "ssn").replacement == "[RESPONDENT-SSN]"
    assert head(store, "petitioner", "phone").replacement == "[PETITIONER-PHONE-1]"
    assert head(store, "child-1", "dob").replacement == "[CHILD-1-DOB]"
    assert head(store, "respondent", "case_number").replacement == "[CASE-NUMBER-1]"
    assert head(store, "respondent", "employer").replacement == "[RESPONDENT-EMPLOYER-1]"
    ssn = head(store, "petitioner", "ssn")
    assert {m.canonical for m in grouping.members(store, ssn)} >= {"XXX-XX-9963", "528419963"}


def test_role_labels_survive_regrouping_and_a_split():
    store = make_store()
    before = {e.key: (e.replacement, e.parent) for e in store.entities.values() if e.fixed}
    grouping.group(store)
    grouping.sync(store)
    grouping._one_replacement_per_head(store)
    after = {e.key: (e.replacement, e.parent) for e in store.entities.values() if e.fixed}
    assert before == after
    phone = head(store, "petitioner", "phone")
    form = next(m for m in grouping.members(store, phone) if m.canonical == "801.555.0143")
    grouping.split(store, form)
    grouping.group(store)
    assert form.parent is None and form.replacement != phone.replacement
    assert phone.replacement == "[PETITIONER-PHONE-1]"
    assert all(m.replacement == "[PETITIONER-PHONE-1]" for m in grouping.members(store, phone))


def test_every_printed_form_is_replaced_with_the_one_label():
    out, _ = run("SSN 528-41-9963, XXX-XX-9963, 528419963 and SSN ending 9963. "
                 "Call 801.555.0143 or +1 801 555 0143.", page=2)
    assert "9963" not in out and "0143" not in out
    assert out.count("[PETITIONER-SSN]") == 4
    assert out.count("[PETITIONER-PHONE-1]") == 2


def test_a_dob_replaces_only_that_date():
    out, _ = run("Born December 6, 1986 (12/06/1986). Hearing on 12/07/1986 and 6/12/1986.",
                 page=2)
    assert out.count("[PETITIONER-DOB]") == 2
    assert "12/07/1986" in out and "6/12/1986" in out


# ----------------------------------------------------------- anchoring --

STATEMENT = """ZIONS BANK
Account holder: JOHN M SMITH
Customer service (801) 555-7000
Statement period 11/01/2024 - 11/30/2024
""" + "\n".join(f"line {n}" for n in range(20)) + """
11/03 ACME PLUMBING 435-555-0100 45.00
11/04 ONLINE TRANSFER TO CHK ****5678 100.00
11/05 COFFEE 12.00 notes 612-44-1234
"""


def test_a_phone_in_the_header_block_is_the_partys_and_a_merchants_is_left():
    out, store = run(STATEMENT, page=0)
    assert "555-7000" not in out
    assert "[RESPONDENT-PHONE-2]" in out          # one holder named in the header
    assert "435-555-0100" in out                  # the merchant's, mid-statement
    assert parties.left_counts(store).get("phone") == 1


def test_a_header_value_with_no_single_holder_is_the_parties():
    text = "Joint statement\nPhone 801-555-7000\n"
    out, _ = run(text, page=0)
    assert "[PARTIES-PHONE-1]" in out


def test_a_valid_ssn_anywhere_and_a_transfer_tail_are_replaced():
    out, store = run(STATEMENT, page=0)
    assert "612-44-1234" not in out and "[PARTIES-SSN-1]" in out
    assert "****5678" not in out and "[PARTIES-ACCOUNT-1]" in out
    # the tail met again later is the same account
    again, _ = run("Paid from ...5678 on 12/01", store, page=4)
    assert "[PARTIES-ACCOUNT-1]" in again


def test_a_value_in_the_block_under_a_partys_name_is_theirs():
    text = "Notes\n\nEmma Rose Smith\n2190 E Ridge Rd, Provo, UT 84604\n"
    out, store = run(text, page=5)
    assert "[CHILD-1-ADDRESS-1]" in out


def test_values_not_anchored_are_left_as_written():
    text = "Reference documents\nThe landlord, 88 E Center St, Provo, UT 84606, called.\n"
    out, store = run(text, page=4)
    assert "88 E Center St" in out
    assert parties.left_counts(store).get("address") == 1


# ------------------------------------------------------------- accounts --

def categories_found(text):
    return {(m.category, m.text) for m in patterns.scan(text)}


def test_phone_shaped_date_shaped_zip_and_references_are_never_accounts():
    accounts = patterns.ACCOUNT_CATEGORIES
    for text in ("Checking 801-555-0143 call us", "Checking account 2021-03-30",
                 "Acct 84041-1234", "Account Transaction 44178825",
                 "Account Ref # 99187263", "Account Check No. 1042"):
        assert not any(c in accounts for c, _v in categories_found(text)), text
    assert ("phone", "801-555-0143") in categories_found("Call 801-555-0143 today")


def test_a_labelled_or_masked_account_is_an_account():
    assert ("bank_account", "000123456789") in categories_found("Account number: 000123456789")
    found = categories_found("Paid with ****1234 today")
    assert any(v == "****1234" for _c, v in found)
    # a phone-shaped account under its own label stays the account
    assert ("bank_account", "801 555 0143") in categories_found("Account number: 801 555 0143")


def test_a_bare_card_shaped_reference_in_a_card_row_is_not_filed():
    out, store = run("12/03 CARD PURCHASE 82633072107526 SUMMIT HOME 14.00", page=4)
    assert "82633072107526" in out
    assert not any(e.canonical == "82633072107526" for e in store.entities.values())


def test_a_known_accounts_digits_written_bare_are_that_account():
    out, _ = run("Zions acct 123456789 and the coupon line 000123456789 and ending 6789",
                 page=4)
    assert "6789" not in out
    assert out.count("[RESPONDENT-ACCOUNT-1]") == 3


def test_leave_categories_are_never_registered():
    out, store = run("Policy number POL-773322 and Claim No. CLM-99812, Loan 7781234",
                     page=0)
    assert "POL-773322" in out and "CLM-99812" in out and "7781234" in out


# ------------------------------------------------------- names and scope --

def test_third_parties_are_left_as_written():
    out, store = run("Zelle payment to Marcus Whitfield for rent. Jane Smith paid.", page=4)
    assert "Marcus Whitfield" in out
    assert "Jane Smith" not in out


def test_the_family_surname_alone_and_inside_a_business():
    store = make_store()
    invented = parties.family_invented(store)["smith"]
    out, _ = run("Smith Landscaping LLC paid Mary Smith; the Smiths agreed.", store, page=4)
    assert "Smith" not in out
    assert f"{invented} Landscaping LLC" in out
    assert f"Mary {invented}" in out
    family = next(e for e in store.entities.values() if e.kind == "family_surname")
    assert family.replacement == invented


def test_names_in_any_order_and_by_piece():
    store = make_store({"parties": [{"role": "respondent",
                                     "names": ["John Quincy Smith", "John Q. Barlow"]}]})
    fake = head(store, "respondent", "name").surrogate
    out, _ = run("Smith, John Q. and SMITH JOHN and John alone and Smith alone. "
                 "Barlow was his name.", store, page=4)
    assert "Smith" not in out and "SMITH" not in out and "John" not in out
    assert f"{fake.last}, {fake.first} Q." in out or f"{fake.last}, {fake.first}" in out
    assert f"{fake.last.upper()} {fake.first.upper()}" in out
    assert "Barlow" not in out


def test_an_everyday_word_piece_only_with_its_capital():
    store = make_store({"parties": [{"role": "petitioner", "names": ["Amber Hope Baker"]}]})
    out, _ = run("the baker said hope was lost; Baker and Hope signed.", store, page=4)
    assert "the baker said hope was lost" in out
    assert "Baker and Hope" not in out


def test_initials_particles_and_suffixes_are_never_searched_alone():
    store = make_store({"parties": [{"role": "petitioner", "names": ["Maria De La Cruz Jr."]}]})
    out, _ = run("De facto, La Cruz was there. La Paz.", store, page=4)
    assert "De facto" in out and "La Paz" in out


def test_ocr_misreadings_and_glued_forms_of_a_piece():
    store = make_store({"parties": [{"role": "petitioner", "names": ["Kayla Harrington"]}]})
    out, _ = run("HARRlNGTON and Harr1ngton and KAYLAHARRINGTON and Harrington", store, page=4)
    assert "arr" not in out.casefold()


def test_a_ticked_misreading_on_the_names_list_is_the_same_person():
    sheet = {"parties": [{"role": "petitioner",
                          "names": ["Kayla Harrington", "Kayla Harringten", "KAYLAHARRINGTON"]}]}
    store = make_store(sheet)
    kayla = head(store, "petitioner", "name")
    forms = grouping.members(store, kayla)
    assert {f.canonical for f in forms} >= {"Kayla Harringten"}
    assert all(f.surrogate.last == kayla.surrogate.last for f in forms
               if f.canonical == "Kayla Harringten")
    assert sum(1 for e in store.entities.values() if e.kind == "name" and e.parent is None) == 1


def test_party_forms_finds_misreadings_former_names_and_the_surname():
    store = make_store()
    texts = {"a.pdf": "Jane Smlth signed. Jane E. Doe, formerly. Smith Landscaping LLC. Janie."}
    forms = parties.party_forms(texts, store)
    why = {(f["text"], f["why"]) for f in forms}
    assert ("Smlth", "OCR misreading") in why
    assert ("Jane E. Doe", "former name") in why
    assert ("Smith Landscaping LLC", "family surname") in why
    assert all(isinstance(f["preticked"], bool) for f in forms)


def test_a_heading_word_after_a_name_is_never_a_form():
    store = make_store({"parties": [{"role": "respondent", "names": ["Derek L Nakamura"]}]})
    texts = {"a.pdf": "Derek L Nakamura Account\nDerek L Nakamura Account Summary\n"
                      "Nakamura Holdings LLC\n"}
    offered = [f["text"] for f in parties.party_forms(texts, store)]
    assert not any("Account" in text for text in offered)
    assert "Nakamura Holdings LLC" in offered
    # and put on the sheet as the window's "Add ticked" would, it loses nothing
    sheet = {"parties": [{"role": "respondent",
                          "names": ["Derek L Nakamura", "Derek L Nakamura Account"]}],
             "family_surnames": ["Nakamura", "Derek L Nakamura Account"]}
    out, store = run("Derek L Nakamura Account opened. Account balance.", make_store(sheet),
                     page=4)
    assert out.count("Account") == 2 and "Nakamura" not in out


def test_a_masked_tail_on_a_loan_or_policy_line_is_left():
    out, _ = run("CREDIT REPORT\n" + "\n".join(f"x {n}" for n in range(20)) +
                 "\nMortgage ****4321 balance 210,000.00\nPolicy ****5678 active\n"
                 "Member ****3456\nTransfer to ****9012 50.00\n", page=0)
    assert "****4321" in out and "****5678" in out and "****3456" in out
    assert "****9012" not in out and "[PARTIES-ACCOUNT-1]" in out


def test_collect_suggestions_proposes_no_third_parties(sample_docx):
    store = make_store()
    found, notes = pipeline.collect_suggestions([sample_docx], store,
                                                engine.Settings(parties_first=True, use_ner=False))
    assert found == [] and notes


# --------------------------------------------------------- review rows --

def test_the_review_groups_are_one_row_per_party_kind():
    store = make_store()
    rows = parties.groups(store)
    labels = [r["label"] for r in rows]
    assert labels.index("Petitioner - SSN") < labels.index("Respondent - SSN") \
        < labels.index("Child 1 - date of birth")
    ssn = next(r for r in rows if r["label"] == "Petitioner - SSN")
    assert ssn["replacement"] == "[PETITIONER-SSN]" and ssn["party"] == "petitioner"
    assert ssn["kind"] == "ssn"
    child = next(r for r in rows if r["label"].startswith("Child 1 - name"))
    assert child["party"] == "child 1"


# ----------------------------------------------------- the rest of the run --

def test_the_store_and_sheet_pickle_for_the_run_child():
    store = make_store()
    again = pickle.loads(pickle.dumps(store))
    assert again.parties.parties[0].names[0] == "Jane Elizabeth Smith"
    assert head(again, "petitioner", "ssn").replacement == "[PETITIONER-SSN]"


def test_the_final_check_reads_for_the_parties_only():
    store = make_store()
    store.add_person("Marcus Whitfield")
    people = audit._ticked_people(store)
    assert "Marcus Whitfield" not in people
    assert "Jane Elizabeth Smith" in people and "Smith" in people
    assert "Emma Rose Smith" in people


def test_legacy_mode_files_everything_as_before():
    store = MappingStore()
    settings = engine.Settings()
    engine.scan_text("Merchant phone 435-555-0100", store, settings)
    assert store.get("phone", "435-555-0100") is not None


def test_the_preparer_works_with_the_sheet(sample_docx, monkeypatch):
    events = queue.Queue()
    from redactor import sets
    # the child's own process group, which this test process must keep
    monkeypatch.setattr(sets, "_own_group", lambda: None)
    from redactor import pdf_processor
    monkeypatch.setattr(pdf_processor, "keep_ocr_on_disk", lambda folder: None)
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "0")
    sheet = parties.PartySheet.from_json(SHEET)
    sets._prepare_set(events, [sample_docx], engine.Settings(parties_first=True, use_ner=False),
                      sample_docx.parent / "spill", sheet)
    kind, prepared = events.get_nowait()
    assert kind == "done", prepared
    assert prepared["suggestions"] == []
    assert isinstance(prepared["party_forms"], list)


def test_a_set_prepared_for_an_older_sheet_is_prepared_again(tmp_path):
    from redactor import sets
    doc_set = sets.DocSet(id="x", name="x", rank=1, folder=tmp_path / "x")
    doc_set.files = [tmp_path / "a.pdf"]
    doc_set.prepared, doc_set.prepared_for = {"suggestions": []}, doc_set._file_set()
    assert doc_set.ready_prepared() is not None
    doc_set.parties_version += 1
    assert doc_set.ready_prepared() is None


# ---------------------------------------------------- the whole run, and the API --

CASE_SHEET = {"parties": [
    {"role": "petitioner", "names": ["Jane Elizabeth Smith"], "ssn": ["528-41-9963"],
     "phones": ["(801) 555-0184"], "dob": ["04/17/1985"]},
    {"role": "respondent", "names": ["John Michael Smith"],
     "case_numbers": ["224900871"]},
]}


def _docx_text(path):
    import zipfile
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def test_a_whole_run_replaces_the_parties_and_leaves_third_parties(sample_docx, tmp_path):
    import zipfile
    store = MappingStore()
    parties.register(parties.PartySheet.from_json(CASE_SHEET), store)
    settings = engine.Settings(parties_first=True, use_ner=False, ocr_scanned_pdfs=False)
    pipeline.prescan([sample_docx], store, settings)
    result = pipeline.run_job([sample_docx], store, settings, tmp_path / "out", "pw")
    assert result.archive is not None and not result.failed
    with zipfile.ZipFile(result.archive) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".docx"))
        (tmp_path / "out.docx").write_bytes(zf.read(member))
    text = _docx_text(tmp_path / "out.docx")
    for secret in ("528-41-9963", "(801) 555-0184", "04/17/1985", "224900871", "Smith"):
        assert secret not in text, secret
    assert "[PETITIONER-SSN]" in text and "[CASE-NUMBER-1]" in text
    # a third party is left as written, and so is the bench
    assert "Wasatch Orthopedic Group" in text
    assert "Amber M. Cordova" in text


@pytest.mark.parametrize("mode", ["redact", "anonymize"])
def test_a_pdf_run_with_the_sheet(sample_pdf, tmp_path, mode):
    import zipfile

    import pymupdf
    store = MappingStore()
    parties.register(parties.PartySheet.from_json(CASE_SHEET), store)
    settings = engine.Settings(parties_first=True, use_ner=False, pdf_mode=mode)
    pipeline.prescan([sample_pdf], store, settings)
    result = pipeline.run_job([sample_pdf], store, settings, tmp_path / "out", "pw")
    assert result.archive is not None and not result.failed
    assert parties.PAGE.get() is None
    with zipfile.ZipFile(result.archive) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".pdf"))
        (tmp_path / "out.pdf").write_bytes(zf.read(member))
    with pymupdf.open(tmp_path / "out.pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    for secret in ("528-41-9963", "224900871", "Smith"):
        assert secret not in text, secret
    assert "Wasatch Orthopedic Group" in text


@pytest.fixture()
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from redactor import webapp

    webapp.SESSION = webapp.Session()
    with TestClient(webapp.app) as test_client:
        test_client.cookies.set(webapp.COOKIE, webapp.TOKEN)
        yield test_client


def _wait(client, job_id, timeout=120):
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


def test_the_parties_api_review_groups_and_edits(client, sample_docx):
    from redactor import webapp

    assert client.get("/api/parties").json() == {"parties": [], "family_surnames": []}
    bad = client.put("/api/parties", json={"parties": [{"role": "respondent",
                                                        "ssn": ["12-34"]}]})
    assert bad.status_code == 422
    assert bad.json()["detail"]["field"] == "ssn"
    assert bad.json()["detail"]["role"] == "respondent"

    with open(sample_docx, "rb") as handle:
        client.post("/api/files", files=[("files", (sample_docx.name, handle,
                                                    "application/octet-stream"))])
    saved = client.put("/api/parties", json=CASE_SHEET).json()
    assert saved["family_surnames"] == ["Smith"]
    assert saved["parties"][0]["ssn"][0]["canonical"] == "528-41-9963"
    assert client.get("/api/parties").json() == saved

    options = {"docx_mode": "anonymize", "ner": False, "ocr": False}
    names = _wait(client, client.post(
        "/api/suggestions", json={"options": options, "names": []}).json()["job"])
    assert names["suggestions"] == [] and isinstance(names["party_forms"], list)

    review = _wait(client, client.post(
        "/api/review", json={"options": options, "names": []}).json()["job"])
    labels = [g["label"] for g in review["groups"]]
    assert "Petitioner - SSN" in labels and "Respondent - name" in labels
    ssn = next(g for g in review["groups"] if g["label"] == "Petitioner - SSN")
    assert ssn["count"] > 0 and ssn["replacement"] == "[PETITIONER-SSN]"
    assert isinstance(review["left_as_written"], dict)
    # nobody but the parties is a row
    assert not any("Wasatch" in e["canonical"] for e in review["entities"])
    assert not webapp.SESSION.current.review_stale

    edited = client.patch("/api/entities", json={"key": ssn["key"], "enabled": False}).json()
    again = next(g for g in edited["groups"] if g["key"] == ssn["key"])
    assert not again["enabled"] and all(not f["enabled"] for f in again["forms"])

    # a changed sheet makes the review list stale until it is built again
    client.put("/api/parties", json=CASE_SHEET)
    assert webapp.SESSION.current.review_stale
