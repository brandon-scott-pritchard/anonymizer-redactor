"""Front-end behaviour: click feedback and the progress modal.

These need a display. Where there is none they skip rather than fail.
"""

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
