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
    assert b'https://github.com/login/device' in response.data
    assert b'Open GitHub authorization manually' in response.data


def test_cli_auth_status_redirects_after_connection(monkeypatch):
    monkeypatch.setattr(app_module, '_connect_github_cli_session', lambda: {'login': 'octocat'})
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/auth/cli/status')

    assert response.get_json() == {
        'status': 'connected',
        'login': 'octocat',
        'redirect': '/prompts',
    }


def test_cli_auth_start_includes_connected_login(monkeypatch):
    monkeypatch.setattr(app_module, '_connect_github_cli_session', lambda: {'login': 'octocat'})

    response = app_module.app.test_client().post('/auth/cli/start')

    assert response.get_json() == {
        'status': 'connected',
        'login': 'octocat',
        'redirect': '/prompts',
    }


def test_cli_auth_start_reuses_waiting_attempt(monkeypatch):
    monkeypatch.setattr(app_module, '_connect_github_cli_session', lambda: None)
    monkeypatch.setattr(app_module, '_start_cli_authentication', lambda: {
        'status': 'waiting', 'message': 'existing', 'output': [],
    })
    with app_module.app.test_client() as client:
        response = client.post('/auth/cli/start', json={'force': True})

    assert response.get_json()['status'] == 'waiting'


def test_cli_authentication_uses_browser_device_flow(monkeypatch):
    captured = {}

    class FakeProcess:
        stdout = iter([
            'one-time code copied\n',
            'Open this URL to continue in your web browser: https://github.com/login/device\n',
            'authorization complete\n',
        ])

        @staticmethod
        def wait():
            return 0

    def fake_popen(command, **kwargs):
        captured['command'] = command
        captured['kwargs'] = kwargs
        return FakeProcess()

    monkeypatch.setattr(app_module, 'find_gh_executable', lambda: 'gh.exe')
    monkeypatch.setattr(app_module, '_github_cli_identity', lambda: {
        'login': 'octocat', 'name': 'Octo Cat', 'avatar_url': 'https://example.test/avatar.png',
    })
    opened_urls = []
    monkeypatch.setattr(app_module.webbrowser, 'open_new_tab', lambda url: opened_urls.append(url) or True)
    monkeypatch.setenv('GH_TOKEN', 'stale-environment-token')
    monkeypatch.setenv('GITHUB_TOKEN', 'another-stale-token')
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
    assert 'GH_TOKEN' not in captured['kwargs']['env']
    assert 'GITHUB_TOKEN' not in captured['kwargs']['env']
    assert app_module._cli_auth_state['status'] == 'complete'
    assert app_module._cli_auth_state['login'] == 'octocat'
    assert app_module._cli_auth_state['output'][-1] == 'authorization complete'
    assert opened_urls == ['https://github.com/login/device']


def test_cli_identity_ignores_environment_token_overrides(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs['env']))
        if command[1:3] == ['auth', 'token']:
            return SimpleNamespace(returncode=0, stdout='persisted-token\n', stderr='')
        return SimpleNamespace(
            returncode=0,
            stdout='{"login":"octocat"}',
            stderr='',
        )

    monkeypatch.setenv('GH_TOKEN', 'stale-environment-token')
    monkeypatch.setenv('GITHUB_TOKEN', 'another-stale-token')
    monkeypatch.setattr(app_module, 'find_gh_executable', lambda: 'gh.exe')
    monkeypatch.setattr(app_module.subprocess, 'run', fake_run)

    assert app_module._github_cli_identity() == {'login': 'octocat'}
    assert 'GH_TOKEN' not in calls[0][1]
    assert 'GITHUB_TOKEN' not in calls[0][1]
    assert calls[1][1]['GH_TOKEN'] == 'persisted-token'
    assert 'GITHUB_TOKEN' not in calls[1][1]


def test_verified_cli_login_replaces_environment_token_for_app_process(monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'stale-environment-token')
    monkeypatch.setenv('GITHUB_TOKEN', 'another-stale-token')
    monkeypatch.setattr(app_module.store, 'upsert_player', lambda *args: None)

    with app_module.app.test_request_context('/'):
        user = app_module._connect_github_cli_user({'login': 'octocat'})

        assert user['login'] == 'octocat'
        assert app_module.session['github_login'] == 'octocat'
        assert app_module.session['github_auth_source'] == 'cli'
        assert 'GH_TOKEN' not in app_module.os.environ
        assert 'GITHUB_TOKEN' not in app_module.os.environ


def test_api_user_does_not_reattach_credentials_after_logout(monkeypatch):
    calls = []
    monkeypatch.setenv('GH_TOKEN', 'configured-environment-token')
    monkeypatch.setattr(
        app_module,
        '_connect_github_cli_session',
        lambda: calls.append('connect') or {'login': 'octocat'},
    )
    monkeypatch.setattr(
        app_module.subprocess,
        'run',
        lambda *args, **kwargs: calls.append('environment token check'),
    )

    with app_module.app.test_client() as client:
        with client.session_transaction() as browser_session:
            browser_session['github_auth_detached'] = True
        response = client.get('/api/user')

    assert response.status_code == 200
    assert response.get_json() == {'login': None}
    assert calls == []
