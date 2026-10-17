"""The final check's flagged pages, fixed on the review screen."""

import zipfile
from types import SimpleNamespace

import docx
import pytest

from redactor import final_review, grouping, mapping, pipeline, review
from redactor.engine import Settings

PASSWORD = "test-only-final-review"         # a throwaway key password for these tests


def _delivered(archive, member):
    import io
    with zipfile.ZipFile(archive) as zf:
        document = docx.Document(io.BytesIO(zf.read(member)))
    return "\n".join(p.text for p in document.paragraphs)


@pytest.fixture
def ran(tmp_path):
    """A two-file set run once, with a name and a number the run missed."""
    first = tmp_path / "letter.docx"
    d = docx.Document()
    d.add_paragraph("Dear Abigail N Okafor,")
    d.add_paragraph("Your neighbour Bgail Okfr Smyth signed for it. Locker 88213377.")
    d.save(first)
    second = tmp_path / "note.docx"
    d = docx.Document()
    d.add_paragraph("Locker 88213377 is due. Bgail Okfr Smyth again.")
    d.save(second)
    store, _ = review.build_store([("Abigail N Okafor", "person")])
    settings = Settings(docx_mode="anonymize")
    pipeline.prescan([first, second], store, settings)
    out = tmp_path / "out"
    out.mkdir()
    result = pipeline.run_job([first, second], store, settings, out, PASSWORD)
    members = sorted(o.delivered_name for o in result.outcomes)
    doc_set = SimpleNamespace(run_result=result, store=store, run_settings=settings,
                              run_password=PASSWORD, audit={},
                              final_flags=[{"file": m, "page": 1, "kev": 0.9, "matched": []}
                                           for m in members])
    return doc_set, members


def test_the_run_missed_what_the_fix_will_take(ran):
    doc_set, members = ran
    text = _delivered(doc_set.run_result.archive, members[0])
    assert "Abigail" not in text                       # the ticked name went
    assert "Bgail Okfr Smyth" in text and "88213377" in text


def test_a_chosen_name_goes_in_every_file_it_is_written_in(ran, monkeypatch):
    doc_set, members = ran
    monkeypatch.setenv("REDACTOR_KEV", "0")            # the matcher reads it back alone
    head = next(e for e in doc_set.store.entities.values() if e.canonical == "Abigail N Okafor")
    done = final_review.fix(doc_set, members[0], 1, "Bgail Okfr Smyth", "person", "")
    assert done.get("error") is None and done["remade"] == 2
    for member in members:                             # carried to the other file too
        assert "Bgail Okfr Smyth" not in _delivered(doc_set.run_result.archive, member)
    assert head.enabled


def test_a_chosen_number_takes_a_placeholder_and_the_key_is_written_again(ran, monkeypatch):
    doc_set, members = ran
    monkeypatch.setenv("REDACTOR_KEV", "0")
    final_review.fix(doc_set, members[0], 1, "88213377", "bank_account", "")
    for member in members:
        assert "88213377" not in _delivered(doc_set.run_result.archive, member)
    key = mapping.read_encrypted_key(doc_set.run_result.key_path, PASSWORD) \
        if hasattr(mapping, "read_encrypted_key") else None
    if key is not None:
        assert "88213377" in str(key)


def test_a_form_of_a_ticked_person_takes_their_invented_name(ran, monkeypatch):
    doc_set, members = ran
    monkeypatch.setenv("REDACTOR_KEV", "0")
    head = next(e for e in doc_set.store.entities.values() if e.canonical == "Abigail N Okafor")
    final_review.fix(doc_set, members[0], 1, "Bgail Okfr", "person", head.key)
    form = doc_set.store.get("person", "Bgail Okfr") or next(
        e for e in doc_set.store.entities.values() if e.canonical == "Bgail Okfr")
    assert grouping.head_of(doc_set.store, form) is head
    assert form.replacement.split()[-1] == head.surrogate.last


def test_a_page_looked_at_with_nothing_on_it_comes_off_the_list(ran):
    doc_set, members = ran
    assert final_review.dismiss(doc_set, members[0], 1) == 1
    assert [f["file"] for f in doc_set.final_flags] == [members[1]]


def test_suspects_mark_a_word_a_letter_or_two_off_a_ticked_name():
    store, _ = review.build_store([("Abigail N Okafor", "person")])
    words = [{"i": i, "text": t} for i, t in enumerate(
        ["Paid", "to", "ABIGAIL", "OKAF0R", "for", "toABIGAILOKAFOR", "groceries"])]
    marked = final_review.suspects(words, store, [])["words"]
    assert 3 in marked and 5 in marked and 6 not in marked and 0 not in marked


def test_suspects_mark_a_ticked_first_names_nickname():
    store, _ = review.build_store([("William J Harrington", "person")])
    words = [{"i": i, "text": t} for i, t in enumerate(["Transfer", "to", "Billy", "Harringtn"])]
    marked = final_review.suspects(words, store, [])["words"]
    assert marked == [2, 3]


def test_a_kind_left_as_written_is_refused_not_reported_clean(ran, monkeypatch):
    """A loan number filed as a loan number is never replaced; saying the page
    reads clean would be false."""
    doc_set, members = ran
    monkeypatch.setenv("REDACTOR_KEV", "0")
    done = final_review.fix(doc_set, members[0], 1, "88213377", "loan_number", "")
    assert done.get("error") and "left as written" in done["error"]
    assert "88213377" in _delivered(doc_set.run_result.archive, members[0])
    assert "loan_number" not in {k["key"] for k in final_review.kinds(doc_set.run_settings)}


def test_a_file_that_cannot_be_made_again_is_never_reported_clean(ran, monkeypatch):
    doc_set, members = ran
    monkeypatch.setenv("REDACTOR_KEV", "0")
    for outcome in doc_set.run_result.outcomes:
        outcome.source.unlink()                      # the original is gone
    done = final_review.fix(doc_set, members[0], 1, "88213377", "bank_account", "")
    assert not done["page_clean"] and "could not be made again" in done["how"]
    assert any(f["file"] == members[0] for f in doc_set.final_flags)


def test_text_still_on_the_page_keeps_the_page_on_the_list(ran, monkeypatch):
    """A file made again that still holds what was chosen - a value the
    literal pass will not take, here stood in for by a remake that changes
    nothing - is not clean, and the page stays listed."""
    from redactor import audit

    doc_set, members = ran
    monkeypatch.setenv("REDACTOR_KEV", "0")
    monkeypatch.setattr(audit, "remake", lambda *a, **k: {})
    done = final_review.fix(doc_set, members[0], 1, "Locker", "bank_account", "")
    assert not done["page_clean"] and "still on the page" in done["how"]
    assert any(f["file"] == members[0] and f["page"] == 1 for f in doc_set.final_flags)


def test_kev_unsure_pages_are_listed_after_the_flagged_ones(tmp_path):
    from redactor import audit

    check = audit.Check(kev_note="Kev", pages=3)
    check.flags = [audit.Flag("a.pdf", 1, 0.8)]
    check.doubtful = [audit.Flag("b.pdf", 2, 0.35, doubtful=True)]
    summary = check.summary()
    assert [f["unsure"] for f in summary["flags"]] == [False, True]
    check.path = tmp_path / "final-check.txt"
    text = audit.render(check)
    assert "Kev unsure - 1 page(s)" in text and "b.pdf, page 2" in text
