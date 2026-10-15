"""The standalone app window.

The React front end, in a real application window - no browser, no tab, no
address bar. The local server still does the work; this just renders it with
the operating system's own web view (WKWebView on macOS, WebView2 on
Windows), which is why the interface looks identical everywhere.

Run with ``python -m redactor.desktop``.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request

from . import __version__
from .webapp import COOKIE, TOKEN, add_paths, app, set_results_root

WINDOW_TITLE = "Document Redactions & Anonymization"
MIN_SIZE = (900, 620)
START_SIZE = (1180, 820)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve(port: int) -> threading.Thread:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def _wait_until_up(port: int, timeout: float = 25.0) -> bool:
    """Poll until the server answers, so the window never opens on a blank page."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError:
            return True            # answering at all means it is listening
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.15)
    return False



def enable_downloads(webview) -> bool:
    """Let the web view save a file. Returns whether the flag is now on.

    pywebview defaults ALLOW_DOWNLOADS to False, and the default moved when the
    pin allowed a major-version jump. Without it every download button on the
    results screen is inert in the app window and fine in a browser, which is a
    miserable thing to debug from a bug report.
    """
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
        return bool(webview.settings.get("ALLOW_DOWNLOADS"))
    except Exception:                       # pragma: no cover - very old pywebview
        return False


class Bridge:
    """What the page can ask of the window itself: a folder picker.

    The window's own file input cannot choose a folder - pywebview answers it
    with a files-only panel - and a folder of several hundred documents is
    better read from disk than uploaded through the page anyway. The page calls
    ``window.pywebview.api.choose_folder()`` and gets the file list back.
    """

    def choose_folder(self, set_id=None):
        """Every set's card has this button; ``set_id`` is the card's set."""
        import webview

        if not webview.windows:
            return None
        chosen = webview.windows[0].create_file_dialog(
            webview.FileDialog.FOLDER, allow_multiple=True)
        if not chosen:
            return None
        return _plainly(add_paths, list(chosen), set_id)

    def choose_results_folder(self):
        """The one folder every queued set's results go to, a folder each."""
        import webview

        if not webview.windows:
            return None
        chosen = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        if not chosen:
            return None
        return _plainly(set_results_root, chosen[0])


def _plainly(call, *args):
    """A refusal reaches the page as its own words, not "422: ..." - the
    bridge hands the page ``str()`` of whatever is raised."""
    from fastapi import HTTPException

    try:
        return call(*args)
    except HTTPException as exc:
        raise RuntimeError(exc.detail) from None


QUIT_WHILE_WORKING = (
    "Sets are still being read or run. Quitting now stops them: a set that was "
    "running is left unfinished and must be run again, and every set's "
    "documents and readings are deleted with the app. Quit anyway?")


def _ask_before_closing_while_working(window) -> None:
    """Closing the window quits the app, and with it every set's reading and
    run. While any is going, the window asks first; otherwise it just closes."""
    import threading
    import time

    from . import webapp

    def watch() -> None:
        while True:
            try:
                window.confirm_close = webapp.SESSION.anything_going()
            except Exception:
                window.confirm_close = True
            time.sleep(1)

    threading.Thread(target=watch, daemon=True, name="close-guard").start()


def main() -> int:
    from . import audit

    # the final check's model: started now, so it is up long before the first
    # set finishes its run, and stopped when the app is (see audit.KevService)
    audit.KEV.start()
    port = _free_port()
    _serve(port)
    if not _wait_until_up(port):
        print("The local server did not start; falling back to the browser.")
        from .webapp import main as web_main
        return web_main()

    try:
        import webview
    except ImportError:
        # no web view available (a bare Linux box, say) - the browser still works
        print("pywebview is not installed; opening in the browser instead.")
        from .webapp import main as web_main
        return web_main()

    # The whole point of the run is the archive at the end of it, and the web
    # view will not save a file unless it is told it may. pywebview ships this
    # off by default, so the three download buttons on the results screen did
    # nothing at all in the app window while working fine in a browser - a
    # plain <a href> to /api/download/archive is a navigation the view drops on
    # the floor. Must be set before the window is created.
    enable_downloads(webview)

    # The token rides in on the query string once; the server then sets its
    # same-site cookie, exactly as it does for a browser.
    window = webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}/?token={TOKEN}",
        width=START_SIZE[0], height=START_SIZE[1],
        min_size=MIN_SIZE,
        resizable=True,
        text_select=True,           # operators need to copy values out
        confirm_close=False,
        js_api=Bridge(),
        localization={"global.quitConfirmation": QUIT_WHILE_WORKING},
    )
    _ask_before_closing_while_working(window)
    print(f"{WINDOW_TITLE} {__version__}")
    print("Everything stays on this computer. Close the window to quit.")
    webview.start()                 # blocks until the window closes
    del window
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
