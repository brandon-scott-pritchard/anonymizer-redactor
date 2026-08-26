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
from .webapp import COOKIE, TOKEN, app

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


def main() -> int:
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
    )
    print(f"{WINDOW_TITLE} {__version__}")
    print("Everything stays on this computer. Close the window to quit.")
    webview.start()                 # blocks until the window closes
    del window
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
