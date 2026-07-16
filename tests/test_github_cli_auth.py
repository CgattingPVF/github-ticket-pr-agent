from __future__ import annotations

from types import SimpleNamespace

import app as app_module


def test_login_renders_persistent_cli_auth_page_without_existing_identity(monkeypatch):
    monkeypatch.delenv('GITHUB_CLIENT_ID', raising=False)
    monkeypatch.setattr(app_module, '_connect_github_cli_session', lambda: None)
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/login')

    assert response.status_code == 200
    assert b'GitHub device authorization' in response.data
    assert b'This page stays open' in response.data


def test_cli_auth_status_redirects_after_connection(monkeypatch):
    monkeypatch.setattr(app_module, '_connect_github_cli_session', lambda: {'login': 'octocat'})
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/auth/cli/status')

    assert response.get_json() == {
        'status': 'connected',
        'login': 'octocat',
        'redirect': '/prompts',
    }


def test_cli_authentication_uses_browser_device_flow(monkeypatch):
    captured = {}

    class FakeProcess:
        stdout = iter(['one-time code copied\n', 'authorization complete\n'])

        @staticmethod
        def wait():
            return 0

    def fake_popen(command, **kwargs):
        captured['command'] = command
        captured['kwargs'] = kwargs
        return FakeProcess()

    monkeypatch.setattr(app_module, 'find_gh_executable', lambda: 'gh.exe')
    monkeypatch.setattr(app_module.subprocess, 'Popen', fake_popen)
    with app_module._cli_auth_lock:
        app_module._cli_auth_state.update({'status': 'starting', 'message': '', 'output': []})

    app_module._run_cli_authentication()

    assert captured['command'] == [
        'gh.exe', 'auth', 'login',
        '--hostname', 'github.com',
        '--git-protocol', 'https',
        '--web',
        '--scopes', 'repo,read:org,project',
    ]
    assert captured['kwargs']['stdin'] is app_module.subprocess.DEVNULL
    assert app_module._cli_auth_state['status'] == 'complete'
    assert app_module._cli_auth_state['output'][-1] == 'authorization complete'
