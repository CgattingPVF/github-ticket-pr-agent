from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path


def configure_bundled_tools() -> None:
    """Expose executables embedded by PyInstaller to child processes."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if bundle_root.joinpath("gh.exe").is_file():
        os.environ["PATH"] = str(bundle_root) + os.pathsep + os.environ.get("PATH", "")


configure_bundled_tools()

from app import app, find_available_port


BROWSER_OPEN_DELAY_SECONDS = 0.75


def build_app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def open_browser_when_ready(url: str) -> None:
    timer = threading.Timer(BROWSER_OPEN_DELAY_SECONDS, webbrowser.open_new_tab, args=(url,))
    timer.daemon = True
    timer.start()


def main() -> None:
    # Respect APP_PORT / APP_HOST for direct launcher.sh usage while keeping
    # the PyInstaller bundled behaviour (auto-detect free port from 3060).
    raw_port = os.getenv("APP_PORT", "").strip()
    try:
        start_port = int(raw_port) if raw_port else 3060
        if not (1 <= start_port <= 65535):
            raise ValueError
    except ValueError:
        start_port = 3060
    # If caller forced a port via --port, prefer it; otherwise auto-detect
    # from start_port upward so --port 3061 actually binds 3061 when free.
    port = find_available_port(start_port=start_port)
    url = build_app_url(port)
    # launcher.sh sets MERGEQUEST_NO_BROWSER=1 to suppress the popup in CI/headless
    if os.getenv("MERGEQUEST_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes", "on"}:
        open_browser_when_ready(url)
    app.run(host=os.getenv("APP_HOST", "127.0.0.1"), port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
