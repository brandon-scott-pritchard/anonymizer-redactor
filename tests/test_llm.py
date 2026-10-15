"""The local model: its service, its merging of the review list, and its
reading of the final check's flagged pages.

A stand-in for mlx_lm.server - a few lines of HTTP server answering the same
two routes - stands in for the real model, so these run anywhere and fast.
What it answers is read, on every request, from a JSON file the test writes.
"""

import json
import os
import queue
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from redactor import (audit, engine, grouping, llm, llm_grouping, pipeline,  # noqa: E402
                      review, sets)
from redactor.mapping import MappingStore  # noqa: E402

FAKE_SERVER = textwrap.dedent('''
    """Answers as mlx_lm.server does, from the answers file the test wrote."""
    import json, os, sys, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    if os.environ.get("FAKE_LLM_EXIT"):
        sys.exit(3)
    time.sleep(float(os.environ.get("FAKE_LLM_DELAY", "0")))
    ANSWERS = os.environ["FAKE_LLM_ANSWERS"]
    asked = {"count": 0}

    def answer(body):
        with open(ANSWERS) as f:
            plan = json.load(f)
        with open(ANSWERS + ".log", "a") as f:
            f.write(json.dumps(body) + "\\n")
        asked["count"] += 1
        sequence = plan.get("sequence", [])
        if asked["count"] <= len(sequence):
            return sequence[asked["count"] - 1]
        system = body["messages"][0]["content"]
        user = body["messages"][-1]["content"]
        if "Group the items" in system:
            return plan.get("review", '{"groups": []}')
        page = user.split("The page:\\n", 1)[-1]
        found = [{"quote": q, "person": p} for q, p in plan.get("quotes", [])
                 if q.lower() in page.lower()]
        found += [{"quote": q, "person": p} for q, p in plan.get("always", [])]
        return "<think>reading</think>" + json.dumps({"found": found})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "fake"}]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = {"choices": [{"message": {"role": "assistant", "content": answer(body)}}]}
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

    port = int(sys.argv[sys.argv.index("--port") + 1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
''')


@pytest.fixture
def answers(tmp_path):
    path = tmp_path / "answers.json"

    def plan(**what):
        path.write_text(json.dumps(what))
        return path
    plan()
    plan.path = path
    plan.log = lambda: [json.loads(line) for line in
                        (path.parent / "answers.json.log").read_text().splitlines()] \
        if (path.parent / "answers.json.log").exists() else []
    return plan


@pytest.fixture
def fake_llm(tmp_path, monkeypatch, answers):
    home = tmp_path / "llm" / "bin"
    (home / "mlx_lm").mkdir(parents=True)
    (home / "mlx_lm" / "__init__.py").write_text("")
    (home / "mlx_lm" / "server.py").write_text(FAKE_SERVER)
    (home / "python").symlink_to(sys.executable)
    monkeypatch.setenv("REDACTOR_LLM_PYTHON", str(home / "python"))
    monkeypatch.setenv("REDACTOR_LLM", "1")
    monkeypatch.setenv("FAKE_LLM_ANSWERS", str(answers.path))
    service = llm.LlmService()
    monkeypatch.setattr(llm, "LLM", service)
    yield service
    service.stop()


def _up(service) -> str:
    assert service.start()
    assert service.ready.wait(30), service.note
    return service.url


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _gone(pid: int, seconds: float = 15) -> bool:
    deadline = time.time() + seconds
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    return not _alive(pid)


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


def test_the_model_starts_offline_answers_and_is_stopped(fake_llm, answers):
    url = _up(fake_llm)
    assert url.startswith("http://127.0.0.1:") and fake_llm.note.startswith("running")
    answers(sequence=['{"ok": true}'])
    assert llm.chat(url, "system", "user", '{"ok": bool}') == {"ok": True}
    sent = answers.log()[0]
    # thinking is off, the answer is deterministic, and no other model is named
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["temperature"] == 0 and "model" not in sent
    pid = fake_llm.process.pid
    fake_llm.stop()
    assert _gone(pid)


def test_the_model_is_started_once_however_often_it_is_asked(fake_llm):
    assert fake_llm.start() and fake_llm.start()
    first = fake_llm.process.pid
    assert fake_llm.start() and fake_llm.process.pid == first


def test_the_model_stops_itself_when_idle_and_starts_again_when_needed(fake_llm, monkeypatch):
    fake_llm.idle_seconds = 1
    _up(fake_llm)
    pid = fake_llm.process.pid
    with fake_llm.held():                      # never stopped while a step asks it
        time.sleep(2.5)
        assert fake_llm.url is not None
    assert _gone(pid, 10), "an idle model stayed resident"
    assert "idle" in fake_llm.note and fake_llm.url is None
    assert _up(fake_llm) and fake_llm.process.pid != pid


def test_the_model_is_stopped_even_when_the_app_dies_without_closing(fake_llm):
    script = textwrap.dedent(f'''
        import os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from redactor import llm
        llm.LLM.start()
        llm.LLM.ready.wait(30)
        print(llm.LLM.process.pid, flush=True)
        os._exit(1)                                  # no atexit, no clean-up
    ''')
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         env=os.environ, timeout=60)
    pid = int(out.stdout.strip().splitlines()[-1])
    assert _gone(pid, 20), "the model outlived the app"


def test_without_the_model_it_says_so_and_does_not_start(monkeypatch, tmp_path):
    monkeypatch.setenv("REDACTOR_LLM", "1")
    monkeypatch.setenv("REDACTOR_LLM_PYTHON", str(tmp_path / "nowhere" / "python"))
    service = llm.LlmService()
    assert not service.start() and service.note.startswith("not installed")
    monkeypatch.setenv("REDACTOR_LLM", "0")
    service = llm.LlmService()
    assert not service.start() and service.note == "turned off (REDACTOR_LLM=0)"


def test_a_model_that_crashed_is_not_waited_for(fake_llm, monkeypatch):
    monkeypatch.setenv("FAKE_LLM_EXIT", "1")
    assert fake_llm.start()
    began = time.time()
    assert llm.wait_for_llm(60) is None
    assert time.time() - began < 15


def test_only_this_computer_is_ever_asked():
    with pytest.raises(llm.LlmError):
        llm.chat("http://example.com:8080", "system", "user")
    with pytest.raises(llm.LlmError):
        llm.chat("http://10.0.0.5:8080", "system", "user")


# --------------------------------------------------------------------------
# reading the model's JSON
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reply", [
    '{"groups": []}',
    '```json\n{"groups": []}\n```',
    '<think>Let me see.\nThe items {are} several.</think>\n{"groups": []}',
    'reasoning without its opening tag</think>{"groups": []}',
    'Here is the answer you asked for:\n{"groups": []}\nHope that helps.',
])
def test_the_json_is_found_however_the_reply_wraps_it(reply):
    assert llm.parse_json(reply) == {"groups": []}


def test_an_answer_that_is_not_json_is_asked_again_once(fake_llm, answers):
    url = _up(fake_llm)
    answers(sequence=["I think they are the same.", '{"groups": []}', "no", "still no"])
    assert llm.chat(url, "system", "user") == {"groups": []}
    assert len(answers.log()) == 2
    with pytest.raises(llm.LlmError):
        llm.chat(url, "system", "user")


# --------------------------------------------------------------------------
# merging the review list
# --------------------------------------------------------------------------


def _store(*people, orgs=()) -> MappingStore:
    """A review list as the scan leaves it: grouped, every row found."""
    store = MappingStore()
    for name, *rest in people:
        options = rest[0] if rest else {}
        entity = store.add_person(name, category=options.get("category", "person"),
                                  role=options.get("role", ""),
                                  distinct=options.get("distinct", False))
        entity.occurrences = options.get("times", 3)
    for org in orgs:
        store.add_value("organization", org).occurrences = 2
    grouping.group(store)
    store.grouped = True
    return store


def _numbers(store) -> dict[str, int]:
    return {e.canonical: i for i, e in enumerate(llm_grouping.candidates(store), 1)}


def _review(answers, store, groups):
    n = _numbers(store)
    answers(review=json.dumps({"groups": [
        {"head": n.get(head, head) if isinstance(head, str) else head,
         "members": [n.get(m, m) if isinstance(m, str) else m for m in members], "why": why}
        for head, members, why in groups]}))


def test_ocr_misreadings_and_nicknames_take_the_persons_invented_name(fake_llm, answers):
    store = _store(("Kyle D Harrington", {"times": 9}), ("Kyle Harrlngton",), ("HARRlNGTON",),
                   ("William Pritchard", {"times": 5}), ("Bill Pritchard",))
    _review(answers, store, [("Kyle D Harrington", ["Kyle Harrlngton", "HARRlNGTON"],
                              "OCR misreading"),
                             ("William Pritchard", ["Bill Pritchard"], "nickname")])
    merges = llm_grouping.propose_merges(store, _up(fake_llm))
    done = llm_grouping.apply_merges(store, merges)
    # the rules had put HARRlNGTON under Kyle Harrlngton: it comes along
    assert [d["form"] for d in done] == ["Kyle Harrlngton", "Bill Pritchard"]
    head = store.entities["person:kyle d harrington"]
    misread = store.get("person", "Kyle Harrlngton")
    surname = store.get("person", "HARRlNGTON")
    assert misread.parent == head.key and surname.parent == head.key
    assert misread.group_note == "merged by the local model: OCR misreading"
    # each form reads as the head's invented name, in the form's own shape
    assert misread.replacement == f"{head.surrogate.first} {head.surrogate.last}"
    assert surname.replacement == head.surrogate.last
    hits = engine.scan_text("Paid HARRlNGTON and Kyle Harrlngton", store,
                            engine.Settings(use_ner=False, pdf_mode="anonymize"))
    assert {h.replacement.casefold() for h in hits} >= {
        head.surrogate.last.casefold(), f"{head.surrogate.first} {head.surrogate.last}".casefold()}
    bill = store.get("person", "Bill Pritchard")
    assert grouping.head_of(store, bill).canonical == "William Pritchard"


def test_two_parties_who_share_a_surname_stay_two_people(fake_llm, answers):
    store = _store(("Jane Elizabeth Smith", {"role": "Petitioner"}),
                   ("John Michael Smith", {"role": "Respondent"}))
    _review(answers, store, [("Jane Elizabeth Smith", ["John Michael Smith"], "same surname")])
    merges = llm_grouping.propose_merges(store, _up(fake_llm))
    assert not any(m.members for m in merges)
    llm_grouping.apply_merges(store, merges)
    jane, john = store.get("person", "Jane Elizabeth Smith"), store.get("person",
                                                                         "John Michael Smith")
    assert jane.parent is None and john.parent is None
    assert jane.replacement != john.replacement


def test_a_child_is_never_merged_with_an_adult(fake_llm, answers):
    store = _store(("Marcus Ashdown",), ("Marcus Ashdown", {"category": "minor"}))
    _review(answers, store, [(1, [2], "the same name")])
    merges = llm_grouping.propose_merges(store, _up(fake_llm))
    assert not any(m.members for m in merges)
    assert any(reason == "a child and an adult" for m in merges for _k, reason in m.refused) \
        or not merges


def test_two_rows_registered_apart_stay_apart(fake_llm, answers):
    store = _store(("Robert Smith", {"distinct": True}), ("Robert J Smith", {"distinct": True}))
    _review(answers, store, [(1, [2], "the same person")])
    llm_grouping.apply_merges(store, llm_grouping.propose_merges(store, _up(fake_llm)))
    assert all(e.parent is None for e in store.entities.values())


def test_numbers_the_model_made_up_are_ignored(fake_llm, answers):
    store = _store(("Kyle D Harrington",), ("Harringten",))
    _review(answers, store, [(99, [1, 2], "x"), ("Kyle D Harrington", [0, 200, "Harringten"],
                                                 "OCR misreading")])
    merges = llm_grouping.propose_merges(store, _up(fake_llm))
    llm_grouping.apply_merges(store, merges)
    assert store.get("person", "Harringten").parent == "person:kyle d harrington"
    assert len(merges) == 1


def test_split_undoes_a_merge_by_the_model_and_a_regrouping_keeps_the_rest(fake_llm, answers):
    store = _store(("Kyle D Harrington",), ("Harringten",), ("HARRlNGTON",))
    _review(answers, store, [("Kyle D Harrington", ["Harringten", "HARRlNGTON"],
                              "OCR misreading")])
    llm_grouping.apply_merges(store, llm_grouping.propose_merges(store, _up(fake_llm)))
    head = store.entities["person:kyle d harrington"]
    wrong = store.get("person", "Harringten")
    grouping.split(store, wrong)
    llm_grouping.settle(store)
    assert wrong.parent is None and wrong.group_note == ""
    assert wrong.replacement != head.replacement and wrong.surrogate.last != head.surrogate.last
    # a regrouping from scratch - a retyped row - neither re-merges the split
    # form nor loses the merge still standing
    grouping.group(store)
    llm_grouping.settle(store)
    assert wrong.parent is None
    assert store.get("person", "HARRlNGTON").parent == head.key
    assert [r["form"] for r in llm_grouping.merged_rows(store)] == ["HARRlNGTON"]


def test_a_business_named_after_a_person_takes_their_invented_surname(fake_llm, answers):
    store = _store(("Kyle D Harrington",), orgs=["Harrington Enterprises"])
    _review(answers, store, [("Kyle D Harrington", ["Harrington Enterprises"],
                              "business named after the person")])
    done = llm_grouping.apply_merges(store, llm_grouping.propose_merges(store, _up(fake_llm)))
    business = store.get("organization", "Harrington Enterprises")
    head = store.entities["person:kyle d harrington"]
    assert business.parent is None                  # not a form of the person
    assert business.replacement == f"{head.surrogate.last} Enterprises"
    assert done and done[0]["business"]
    # an edited invented name carries to the business
    review.set_replacement(head, "Ezra Q Vantongeren", store)
    llm_grouping.settle(store)
    assert business.replacement == "Vantongeren Enterprises"


def test_a_long_list_is_asked_in_parts_without_cutting_a_family(fake_llm, answers):
    people = [(f"Alma {chr(66 + i // 6)}{'aeiou'[i % 5]}{chr(98 + i % 6)}stone",)
              for i in range(40)]
    store = _store(*people, ("Kyle D Harrington",), ("Harringten",), ("HARRlNGTON",))
    parts = llm_grouping.chunks(llm_grouping.candidates(store), size=10, overlap=3)
    assert len(parts) > 3
    harringtons = {"Kyle D Harrington", "Harringten", "HARRlNGTON"}
    assert any(harringtons <= {e.canonical for e in part} for part in parts)
    assert all(not ({e.canonical for e in part} & harringtons)
               or harringtons <= {e.canonical for e in part} for part in parts)


def test_the_review_says_when_the_model_was_not_used(monkeypatch):
    from redactor import webapp

    monkeypatch.setenv("REDACTOR_LLM", "0")
    monkeypatch.setattr(llm, "LLM", llm.LlmService())
    store = _store(("Kyle D Harrington",), ("Harringten",))
    webapp._merge_with_llm(store, lambda *a: None)
    assert "not used" in store.merge_note and "REDACTOR_LLM=0" in store.merge_note
    assert store.get("person", "Harringten").parent is None


def test_the_review_step_asks_the_model_and_reports_what_it_merged(fake_llm, answers):
    from redactor import webapp

    store = _store(("Kyle D Harrington",), ("Harringten",))
    _review(answers, store, [("Kyle D Harrington", ["Harringten"], "OCR misreading")])
    said = []
    webapp._merge_with_llm(store, lambda message, fraction: said.append(message))
    assert "Asking the local model which names are the same person" in said
    assert "merged 1 name(s)" in store.merge_note
    assert llm_grouping.merged_rows(store) == [
        {"form": "Harringten", "head": "Kyle D Harrington", "why": "OCR misreading"}]
    assert fake_llm.holders == 0                    # let go: it may stop when idle


# --------------------------------------------------------------------------
# the flagged pages
# --------------------------------------------------------------------------

SETTINGS = dict(pdf_mode="anonymize", use_ner=False)


def _run(tmp_path, lines: list[str], name="statement.pdf"):
    source = tmp_path / name
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "\n".join(lines), fontsize=11)
    doc.save(source)
    store, _ = review.build_store([("Kyle D Harrington", "person")])
    settings = engine.Settings(**SETTINGS)
    pipeline.prescan([source], store, settings)
    out = tmp_path / "results"
    result = pipeline.run_job([source], store, settings, out, "")
    return source, store, settings, out, result


def _delivered(result) -> str:
    with zipfile.ZipFile(result.archive) as zf:
        member = next(m for m in zf.namelist() if m.endswith(".pdf"))
        with pymupdf.open(stream=zf.read(member)) as doc:
            return "".join(page.get_text() for page in doc)


def _flagged(result, out, store):
    """The final check as it stands before the model reads: the one page,
    flagged by Kev (not running here, so its say is put in by hand)."""
    check = audit.check_archive(result.archive, store, None, out, None)
    with zipfile.ZipFile(result.archive) as zf:
        member = next(m for m in zf.namelist() if m.endswith(".pdf"))
    check.flags = check.flags or [audit.Flag(member, 1, 0.9, [], text=_delivered(result))]
    return check


def test_a_misread_name_is_fixed_and_the_page_read_back_clean(fake_llm, answers, tmp_path):
    source, store, settings, out, result = _run(
        tmp_path, ["Letter to Kyle Harrlngton about the house", "Paid in full"])
    assert "Harrlngton" in _delivered(result)       # the run missed it
    check = _flagged(result, out, store)
    answers(quotes=[["Harrlngton", "1"]])
    fixed = audit.read_flagged(check, result, store, settings, out, None, _up(fake_llm))
    head = store.entities["person:kyle d harrington"]
    text = _delivered(result)
    assert "Harrlngton" not in text and head.surrogate.last in text
    assert [(f.file, f.page) for f in check.fixed] == [(check.flags[0].file, 1)]
    assert check.still == [] and check.summary()["fixed"] == 1
    assert fixed is not None and fixed.get("person", "Harrlngton").parent == head.key
    written = check.path.read_text()
    assert "Fixed by the local model and rechecked - 1 page(s)" in written
    assert '"Harrlngton" (Kyle D Harrington)' in written


def test_a_name_glued_into_a_bank_line_is_fixed_keeping_the_rest(fake_llm, answers, tmp_path):
    source, store, settings, out, result = _run(
        tmp_path, ["Deposit 4417IntuPymdHARRINGTONENTERPRISES 2,485.81"])
    assert "HARRINGTON" in _delivered(result)
    check = _flagged(result, out, store)
    answers(quotes=[["HARRINGTONENTERPRISES", "Kyle D Harrington"]])
    audit.read_flagged(check, result, store, settings, out, None, _up(fake_llm))
    text = _delivered(result)
    head = store.entities["person:kyle d harrington"]
    assert "HARRINGTON" not in text.upper().replace(head.surrogate.last.upper(), "")
    assert "4417IntuPymd" in text and "2,485.81" in text
    assert len(check.fixed) == 1


def test_a_quote_that_is_not_on_the_page_is_never_applied(fake_llm, answers, tmp_path):
    source, store, settings, out, result = _run(tmp_path, ["Letter to Kyle Harrington"])
    before = Path(result.archive).read_bytes()
    check = _flagged(result, out, store)
    answers(always=[["Kyle Harrington", "1"]])      # the page reads the invented name
    fixed = audit.read_flagged(check, result, store, settings, out, None, _up(fake_llm))
    assert fixed is None and not check.fixed
    assert Path(result.archive).read_bytes() == before
    assert len(check.still) == 1 and "were not on the page" in check.still[0].note
    assert "Still to look at - 1 page(s)" in check.path.read_text()


def test_a_page_that_cannot_be_made_again_is_left_to_look_at(fake_llm, answers, tmp_path):
    source, store, settings, out, result = _run(tmp_path, ["Letter to Kyle Harrlngton"])
    check = _flagged(result, out, store)
    source.unlink()                                  # its original is gone
    answers(quotes=[["Harrlngton", "1"]])
    audit.read_flagged(check, result, store, settings, out, None, _up(fake_llm))
    assert not check.fixed and len(check.still) == 1
    written = check.path.read_text()
    assert "Still to look at" in written
    assert '"Harrlngton" (Kyle D Harrington) but could not fix it' in written
    assert check.summary()["still"] == 1


def test_a_flag_the_model_cannot_confirm_is_never_cleared_until_allowed(fake_llm, answers,
                                                                       tmp_path, monkeypatch):
    source, store, settings, out, result = _run(tmp_path, ["Payment of 20.00 received"])
    check = _flagged(result, out, store)
    answers()                                        # the model finds nothing
    url = _up(fake_llm)
    audit.read_flagged(check, result, store, settings, out, None, url)
    assert audit.LLM_MAY_CLEAR is False
    assert len(check.still) == 1 and not check.cleared
    assert "Cleared by the local model" not in check.path.read_text()
    monkeypatch.setattr(audit, "LLM_MAY_CLEAR", True)
    check = _flagged(result, out, store)
    audit.read_flagged(check, result, store, settings, out, None, url)
    assert len(check.cleared) == 1 and check.still == []
    assert "Cleared by the local model - 1 page(s)" in check.path.read_text()


def test_without_the_model_the_flagged_pages_are_listed_as_before(tmp_path):
    source, store, settings, out, result = _run(tmp_path, ["Letter to Kyle Harrlngton"])
    check = _flagged(result, out, store)
    events, replies = queue.Queue(), queue.Queue()
    replies.put((None, "not installed (no python)"))
    sets._read_flagged(events, replies, check, result, store, settings, out, "",
                       lambda *a: None)
    assert events.get_nowait() == ("need", "llm")
    assert check.still is None and check.summary()["still"] == 1
    written = check.path.read_text()
    assert "1 page(s) to look at" in written and "not installed" in written
    # and with no channel to ask on - a caller from before - nothing changes at all
    check = _flagged(result, out, store)
    sets._read_flagged(events, None, check, result, store, settings, out, "",
                       lambda *a: None)
    assert check.still is None and check.llm_note == ""


# --------------------------------------------------------------------------
# one model at a time
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_slot(monkeypatch):
    from redactor import models

    slot = models.ModelSlot()
    monkeypatch.setattr(models, "SLOT", slot)
    return slot


@pytest.fixture
def fake_kev(tmp_path, monkeypatch):
    from test_audit import FAKE_SERVE

    home = tmp_path / "kev"
    (home / "kev").mkdir(parents=True)
    (home / "kev" / "__init__.py").write_text("")
    (home / "kev" / "serve.py").write_text(FAKE_SERVE)
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / ".venv" / "bin" / "python").symlink_to(sys.executable)
    monkeypatch.setenv("REDACTOR_KEV_DIR", str(home))
    monkeypatch.delenv("REDACTOR_KEV", raising=False)
    service = audit.KevService()
    monkeypatch.setattr(audit, "KEV", service)
    yield service
    service.stop()


def test_the_slot_never_has_both_models_loaded(fresh_slot, fake_kev, fake_llm):
    with fresh_slot.use("kev") as kev_url:
        assert kev_url and fake_kev.url == kev_url
        kev_pid = fake_kev.process.pid
    with fresh_slot.use("llm") as llm_url:
        assert llm_url and not _alive(kev_pid)      # Kev was gone before it started
        llm_pid = fake_llm.process.pid
    with fresh_slot.use("kev") as kev_url:
        assert kev_url and not _alive(llm_pid)


def test_steps_waiting_for_the_slot_are_served_in_the_order_they_asked(fresh_slot):
    import threading

    order, waited = [], []
    first = fresh_slot.acquire()

    def step(n):
        lease = fresh_slot.acquire(on_wait=lambda: waited.append(n))
        order.append(n)
        lease.release()

    threads = []
    for n in range(4):
        threads.append(threading.Thread(target=step, args=(n,)))
        threads[-1].start()
        time.sleep(0.2)                              # each asks after the one before
    assert order == [] and waited == [0, 1, 2, 3]
    first.release()
    for thread in threads:
        thread.join(5)
    assert order == [0, 1, 2, 3]


def test_the_slot_stops_its_model_when_nobody_has_asked_for_a_while(fake_llm):
    from redactor import models

    slot = models.ModelSlot(idle_seconds=1)
    with slot.use("llm") as url:
        assert url
        pid = fake_llm.process.pid
        time.sleep(1.5)                              # never while in use
        assert _alive(pid)
    assert _gone(pid, 10)


def test_a_model_that_cannot_start_is_answered_with_none_and_why(fresh_slot, monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv("REDACTOR_LLM", "1")
    monkeypatch.setenv("REDACTOR_LLM_PYTHON", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(llm, "LLM", llm.LlmService())
    doc_set = sets.DocSet(id="a", name="A", rank=1, folder=tmp_path / "a")
    turn = sets._Turn()
    url, note = turn.load(doc_set, "llm")
    turn.release()
    assert url is None and note.startswith("not installed")
    assert not fresh_slot.busy


def test_the_run_asks_the_parent_for_each_model_and_carries_on_without_one():
    events, replies = queue.Queue(), queue.Queue()
    replies.put(("http://127.0.0.1:9", ""))
    assert sets._need(events, replies, "kev", lambda *a: None) == ("http://127.0.0.1:9", "")
    assert events.get_nowait() == ("need", "kev")
    replies.put((None, "turned off (REDACTOR_LLM=0)"))
    assert sets._need(events, replies, "llm", lambda *a: None) == (
        None, "turned off (REDACTOR_LLM=0)")


def test_a_queued_run_has_kev_then_the_model_never_both(fake_kev, fake_llm, answers, tmp_path):
    """The whole sequence through a real run child: Kev reads every page, is
    stopped, and the local model fixes the glued name Kev flagged."""
    import threading

    source, store, settings, out, result = _run(
        tmp_path, ["Deposit 4417IntuPymdHARRINGTONENTERPRISES 2,485.81"])
    doc_set = sets.DocSet(id="a", name="Case A", rank=1, folder=tmp_path / "set")
    doc_set.files, doc_set.store = [source], store
    answers(quotes=[["HARRINGTONENTERPRISES", "1"]])
    both, watching = [], [True]

    def watch():
        while watching[0]:
            k, m = fake_kev.process, fake_llm.process
            if k is not None and m is not None and k.poll() is None and m.poll() is None:
                both.append(time.time())
            time.sleep(0.05)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    runs = sets.RunQueue(tmp_path / "spill")
    try:
        runs._run(doc_set, settings, "pw", tmp_path / "queued")
    finally:
        watching[0] = False
        watcher.join(2)
    assert doc_set.run_state == "done", doc_set.run_error
    assert not both, "Kev and the local model were loaded together"
    summary = doc_set.audit
    assert summary["flagged"] == 1 and summary["fixed"] == 1 and summary["still"] == 0, summary
    assert summary["kev"].startswith("Kev")
    archive = next(doc_set.results.glob("*.zip"))
    with zipfile.ZipFile(archive) as zf:
        member = next(m for m in zf.namelist() if m.endswith(".pdf"))
        with pymupdf.open(stream=zf.read(member)) as doc:
            text = doc[0].get_text()
    stand_in = store.entities["person:kyle d harrington"].surrogate.last.upper()
    assert "HARRINGTON" not in text.upper().replace(stand_in, "")
    written = (doc_set.results / "final-check.txt").read_text()
    assert "Fixed by the local model and rechecked - 1 page(s)" in written
