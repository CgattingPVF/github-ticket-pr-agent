from __future__ import annotations

import launcher


def test_build_app_url() -> None:
    assert launcher.build_app_url(3060) == "http://127.0.0.1:3060"


def test_open_browser_when_ready_schedules_daemon_timer(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeTimer:
        def __init__(self, interval, function, args=()):
            captured["interval"] = interval
            captured["function"] = function
            captured["args"] = args
            self.daemon = False

        def start(self) -> None:
            captured["daemon"] = self.daemon
            captured["started"] = True

    monkeypatch.setattr(launcher.threading, "Timer", FakeTimer)

    launcher.open_browser_when_ready("http://127.0.0.1:3060")

    assert captured == {
        "interval": launcher.BROWSER_OPEN_DELAY_SECONDS,
        "function": launcher.webbrowser.open_new_tab,
        "args": ("http://127.0.0.1:3060",),
        "daemon": True,
        "started": True,
    }
