"""Front-end behaviour: click feedback and the progress modal.

These need a display. Where there is none they skip rather than fail.
"""

import gc
import time

import pytest

tk = pytest.importorskip("tkinter")
ttk = pytest.importorskip("tkinter.ttk")


@pytest.fixture
def app():
    from redactor.gui import App
    try:
        window = App()
    except tk.TclError as exc:                     # pragma: no cover - headless
        pytest.skip(f"no display available: {exc}")
    window.geometry("1120x780+60+60")
    window.update_idletasks()
    window.update()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass
    # Drain Tk's finalizers here, on purpose, while we still control when they
    # run. Every StringVar the window made has a __del__ that talks to the Tk
    # interpreter, and the interpreter is now gone - so left to the garbage
    # collector they fire at some arbitrary later moment. One of those moments
    # was inside an `import numpy` on a background thread, which held the
    # import lock while it blocked, and the webapp's own thread was waiting on
    # that same lock to import pytesseract. The suite deadlocked for six and a
    # half minutes at one percent CPU. Collecting here makes them fire now,
    # where the error is caught and harmless.
    for _ in range(3):
        try:
            gc.collect()
        except Exception:                          # pragma: no cover - defensive
            break


def pump(window, seconds=1.0, until=None):
    deadline = time.time() + seconds
    while time.time() < deadline:
        window.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else False


def all_buttons(widget):
    if isinstance(widget, ttk.Button):
        yield widget
    for child in widget.winfo_children():
        yield from all_buttons(child)


def test_the_theme_honours_colour_changes(app):
    """macOS aqua ignores button colours, which would make the flash invisible."""
    assert app.tk.call("ttk::style", "theme", "use") == "clam"


def test_a_click_flashes_the_button_then_restores_it(app):
    button = app.run_button
    resting = str(button.cget("style"))

    ran = []
    button.configure(command=lambda: app._clicked(button, lambda: ran.append(1)))
    button.invoke()
    app.update()

    assert str(button.cget("style")) == "Flash.TButton"
    assert pump(app, 1.0, lambda: str(button.cget("style")) != "Flash.TButton"), \
        "the flash never cleared"
    assert str(button.cget("style")) == resting
    assert pump(app, 1.0, lambda: bool(ran)), "the button's command never ran"


def test_every_button_goes_through_the_flash(app):
    buttons = list(all_buttons(app))
    assert len(buttons) > 15
    assert all(str(b.cget("command")) for b in buttons)


def test_the_progress_modal_reports_and_keeps_ticking(app):
    from redactor.gui import ProgressDialog

    dialog = ProgressDialog(app, "Processing")
    dialog.update_progress("Redacting page 3 of 14", 0.42)
    app.update()
    dialog.update()

    assert dialog.winfo_viewable()
    assert dialog.grab_current() == dialog, "the dialog is not modal"
    assert dialog.message.cget("text") == "Redacting page 3 of 14"
    assert float(dialog.bar.cget("value")) == pytest.approx(42.0)

    # the elapsed indicator must move even while a step reports no progress,
    # otherwise a slow page is indistinguishable from a hang
    first = dialog.detail.cget("text")
    assert pump(app, 2.0, lambda: dialog.detail.cget("text") != first), \
        "the heartbeat is not animating"

    dialog.close()
    app.update()
    assert app.grab_current() is None, "the grab outlived the dialog"


def test_the_heartbeat_never_goes_blank(app):
    """A blank indicator reads as 'stopped', which is the opposite of the point."""
    from redactor.gui import ProgressDialog

    dialog = ProgressDialog(app, "Processing")
    seen = set()
    for _ in range(8):
        dialog._tick()
        seen.add(dialog.detail.cget("text").split()[0])
    assert all(marker.strip() for marker in seen)
    dialog.close()


def test_the_modal_cannot_be_dismissed_by_the_user(app):
    from redactor.gui import ProgressDialog

    dialog = ProgressDialog(app, "Processing")
    assert dialog.protocol("WM_DELETE_WINDOW"), "closing the window is not intercepted"
    dialog.close()


# ------------------------------------------------------ checkboxes & clicks --

from types import SimpleNamespace


def _review_row(app):
    """Populate the review tree with one person and return (store, iid)."""
    from redactor.mapping import MappingStore

    store = MappingStore()
    store.add_person("John Michael Smith")
    app._review_ready(store)
    app._unlock(app.tab_review)
    app.notebook.select(app.tab_review)
    app.update_idletasks()
    app.update()
    return store, app.review_tree.get_children()[0]


def _cell_event(tree, iid, column):
    x, y, w, h = tree.bbox(iid, column)
    return SimpleNamespace(x=x + w // 2, y=y + h // 2)


def test_the_rows_are_tall_enough_to_click(app):
    style = ttk.Style(app)
    assert int(style.lookup("Big.Treeview", "rowheight")) >= 28
    assert str(app.review_tree.cget("style")) == "Big.Treeview"
    assert str(app.suggest_tree.cget("style")) == "Big.Treeview"


def test_double_click_on_the_type_column_toggles(app):
    store, iid = _review_row(app)
    entity = store.entities[iid]
    assert entity.enabled
    app._review_double(_cell_event(app.review_tree, iid, "#1"))
    assert not entity.enabled
    app._review_double(_cell_event(app.review_tree, iid, "#1"))
    assert entity.enabled


def test_double_click_on_the_times_column_is_inert(app, monkeypatch):
    store, iid = _review_row(app)
    entity = store.entities[iid]
    opened = []
    monkeypatch.setattr(app, "_open_row_editor",
                        lambda **k: opened.append(1) or None)
    before = entity.enabled
    app._review_double(_cell_event(app.review_tree, iid, "#4"))
    assert entity.enabled == before
    assert not opened, "the editor must not open from the Times column"


def test_double_click_on_found_opens_the_editor_and_cancel_changes_nothing(app, monkeypatch):
    from redactor import feedback

    store, iid = _review_row(app)
    entity = store.entities[iid]
    original = entity.replacement
    monkeypatch.setattr(app, "_open_row_editor", lambda **k: None)
    app._review_double(_cell_event(app.review_tree, iid, "#2"))
    assert entity.replacement == original, "Cancel must not clear the replacement"

    monkeypatch.setattr(app, "_open_row_editor",
                        lambda **k: ("Person name", "Tamsin Q. Middleton", feedback.NO_ERROR))
    app._review_double(_cell_event(app.review_tree, iid, "#2"))
    assert entity.replacement == "Tamsin Q. Middleton"


def test_a_click_on_a_suggestion_name_cell_does_not_toggle(app):
    from redactor.caption import CaptionName

    app.caption_names = [CaptionName("Jane Ellen Smith", "Petitioner", "doc", "medium")]
    app._render_suggestions()
    app._unlock(app.tab_names)
    app.notebook.select(app.tab_names)
    app.update_idletasks()
    app.update()
    iid = app.suggest_tree.get_children()[0]
    assert iid not in app._suggest_checked

    app._suggest_click(_cell_event(app.suggest_tree, iid, "#1"))
    assert iid not in app._suggest_checked, "a name-cell click must not toggle"

    app._suggest_click(_cell_event(app.suggest_tree, iid, "#0"))
    assert iid in app._suggest_checked


# --------------------------------------------------- type change & reports --

def test_saving_a_new_type_reregisters_the_entity(app, monkeypatch):
    from redactor import feedback

    store, iid = _review_row(app)
    monkeypatch.setattr(app, "_open_row_editor",
                        lambda **k: ("Organization / employer / school",
                                     k.get("replacement"), feedback.NO_ERROR))
    app._review_double(_cell_event(app.review_tree, iid, "#2"))

    keys = list(store.entities)
    assert len(keys) == 1
    assert store.entities[keys[0]].category == "organization"
    assert store.entities[keys[0]].canonical == "John Michael Smith"


def test_an_error_report_is_logged_and_the_email_offered(app, monkeypatch, tmp_path):
    import redactor.gui as gui_mod
    from redactor import feedback

    store, iid = _review_row(app)
    logged = []
    opened = []
    monkeypatch.setattr(feedback, "log_report",
                        lambda report: logged.append(report) or tmp_path / "feedback.jsonl")
    monkeypatch.setattr(gui_mod.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(gui_mod.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(app, "_open_row_editor",
                        lambda **k: ("Organization / employer / school",
                                     k.get("replacement"),
                                     "Person flagged as organization"))
    app._review_double(_cell_event(app.review_tree, iid, "#2"))

    assert len(logged) == 1
    assert logged[0]["predicted_category"] == "person"
    assert logged[0]["corrected_category"] == "organization"
    assert len(opened) == 1
    assert opened[0].startswith(f"mailto:{feedback.REPORT_ADDRESS}")


def test_suggestion_type_change_updates_the_row_and_the_name_list(app, monkeypatch):
    from redactor import feedback
    from redactor.caption import CaptionName

    app.caption_names = [CaptionName("Jane Ellen Smith", "Petitioner", "doc", "high")]
    app._render_suggestions()
    app._unlock(app.tab_names)
    app.notebook.select(app.tab_names)
    app.update_idletasks()
    app.update()
    iid = app.suggest_tree.get_children()[0]

    monkeypatch.setattr(app, "_open_row_editor",
                        lambda **k: ("Minor child", None, feedback.NO_ERROR))
    app._suggest_double(_cell_event(app.suggest_tree, iid, "#2"))

    assert app._suggest_meta[iid] == ("Jane Ellen Smith", "minor")
    app._suggest_checked.add(iid)
    app.add_checked_suggestions()
    assert "Jane Ellen Smith | minor" in app.names_text.get("1.0", "end")


# ------------------------------------------------------------ wizard chrome --

def test_later_tabs_start_locked_and_unlock_in_order(app):
    for tab in (app.tab_names, app.tab_review, app.tab_run):
        assert app.notebook.tab(tab, "state") == "disabled"
    # selecting a locked tab must not switch to it
    app.notebook.select(app.tab_review)
    assert app.notebook.index("current") == 0
    app._unlock(app.tab_names)
    app.notebook.select(app.tab_names)
    assert app.notebook.index("current") == 1


def test_review_rows_are_zebra_striped(app):
    from redactor.mapping import MappingStore

    store = MappingStore()
    for name in ("Jane Elizabeth Smith", "John Michael Smith", "Tommy Smith"):
        store.add_person(name)
    app._review_ready(store)
    rows = app.review_tree.get_children()
    stripes = [app.review_tree.item(iid, "tags") for iid in rows]
    assert list(stripes[0]) == [] and "stripe" in stripes[1]


# ------------------------------------------------- review-decision safety --

def test_rescan_preserves_review_decisions(app):
    from redactor.mapping import MappingStore

    store, iid = _review_row(app)
    store.entities[iid].enabled = False
    carry = {e.canonical.casefold(): (e.enabled, e.category, e.replacement)
             for e in store.entities.values()}

    fresh = MappingStore()
    fresh.add_person("John Michael Smith")
    app._carry_decisions(fresh, carry)
    survivor = next(iter(fresh.entities.values()))
    assert not survivor.enabled, "an untick must survive a rescan"


def test_retype_to_person_rejects_digit_strings(app):
    from redactor.mapping import MappingStore

    store = MappingStore()
    entity = store.add_value("ssn", "528-41-9963")
    app.store = store
    assert app._retype_entity(entity, "person") is None
    assert entity.key in store.entities, "a refused retype must change nothing"


def test_retype_merges_into_an_existing_entity(app):
    from redactor.mapping import MappingStore

    store = MappingStore()
    moved = store.add_value("pob", "Salt Lake City")
    target = store.add_value("location", "Salt Lake City")
    target.enabled = False
    moved.occurrences = 3
    app.store = store

    survivor = app._retype_entity(moved, "location")
    assert survivor is target
    assert survivor.occurrences == 3
    assert not survivor.enabled, "the survivor's tick must not be clobbered"
    assert moved.key not in store.entities


def test_double_click_on_the_checkbox_nets_one_toggle(app):
    store, iid = _review_row(app)
    entity = store.entities[iid]
    before = entity.enabled
    # the real Tk sequence: press one fires <Button-1>, press two fires ONLY
    # <Double-1> - not a second <Button-1>
    app._review_click(_cell_event(app.review_tree, iid, "#0"))
    app._review_double(_cell_event(app.review_tree, iid, "#0"))
    assert entity.enabled != before


def test_suggestion_ticks_survive_a_rerender(app):
    from redactor.caption import CaptionName

    app.caption_names = [
        CaptionName("Jane Ellen Smith", "Petitioner", "doc", "high"),
        CaptionName("Rob Deakins", "Attorney", "doc", "medium"),
    ]
    app._render_suggestions()
    rows = app.suggest_tree.get_children()
    high, medium = rows[0], rows[1]
    assert high in app._suggest_checked

    app._suggest_toggle(high)      # operator unticks the default
    app._suggest_toggle(medium)    # and ticks the medium one
    app._render_suggestions()      # a re-read must not undo either
    assert high not in app._suggest_checked
    assert medium in app._suggest_checked


def test_running_with_an_emptied_password_is_refused(app, monkeypatch):
    import redactor.gui as gui_mod

    warned = []
    monkeypatch.setattr(gui_mod.messagebox, "showwarning",
                        lambda *a, **k: warned.append(a))
    app.files = [__import__("pathlib").Path("x.docx")]
    app.key_password.set("   ")
    started = []
    monkeypatch.setattr(app, "_work", lambda *a, **k: started.append(1))
    app.execute()
    assert warned and not started


def test_the_bench_and_folded_names_reach_the_settings_and_the_screen(app):
    """Step 2 has to show what it decided, and the run has to receive it."""
    from redactor import officials

    app._unlock(app.tab_names)
    app.judicial_officers = officials.harvest("Judge Amber M. Cordova")
    app.names_text.insert("1.0", "Jane Elizabeth Smith\nSmith")

    store = app._store_with_names()
    assert len(store.persons()) == 1, "the bare surname must fold into the full name"

    app._refresh_protection()
    assert "Amber M. Cordova" in app.protection.terms
    assert app.settings().protected_names, "the run must be told what to leave alone"

    shown = app.guard_label.cget("text")
    assert "Cordova" in shown
    assert "Smith" in shown, "the fold has to be said out loud, not done quietly"


def test_a_judge_sharing_a_party_surname_is_only_partly_shielded(app):
    from redactor import officials

    app._unlock(app.tab_names)
    app.judicial_officers = officials.harvest("Judge Amber M. Smith")
    app.names_text.insert("1.0", "Jane Elizabeth Smith")
    app._refresh_protection()

    assert "Amber M. Smith" in app.protection.terms
    assert "Smith" not in app.protection.terms
    assert "shares a surname" in app.guard_label.cget("text")
