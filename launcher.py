from __future__ import annotations

import threading
import webbrowser

from app import app, find_available_port


BROWSER_OPEN_DELAY_SECONDS = 0.75


def build_app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def open_browser_when_ready(url: str) -> None:
    timer = threading.Timer(BROWSER_OPEN_DELAY_SECONDS, webbrowser.open_new_tab, args=(url,))
    timer.daemon = True
    timer.start()


def main() -> None:
    port = find_available_port()
    url = build_app_url(port)
    open_browser_when_ready(url)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
