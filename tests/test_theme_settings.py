from __future__ import annotations

import re

import pytest

import app as app_module


def _theme_values(html: bytes) -> list[bytes]:
    return re.findall(rb'<input[^>]+name="theme"[^>]+value="([^"]+)"', html).copy()


def test_settings_defaults_to_cyberpunk_and_lists_supported_themes():
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/settings')

    assert response.status_code == 200
    assert b'<html lang="en" data-theme="cyberpunk">' in response.data
    assert b'name="theme" value="cyberpunk" checked' in response.data
    assert _theme_values(response.data) == [b'cyberpunk', b'wwii', b'hacker', b'skyrim', b'professional', b'pvf']
    assert b'Cyberpunk 2077' in response.data
    assert b'World War II' in response.data
    assert b'Hacker' in response.data
    assert b'Skyrim' in response.data
    assert b'Professional Work' in response.data
    assert b'theme-wwii.css' not in response.data
    assert b'theme-hacker.css' not in response.data
    assert b'theme-skyrim.css' not in response.data
    assert b'theme-professional.css' not in response.data


def test_settings_can_clear_the_entire_persisted_queue(tmp_path, monkeypatch):
    store = app_module.JobStore(tmp_path / 'jobs.sqlite3')
    first = store.create({'issue_url': 'https://github.com/acme/one/issues/1', 'base_branch': 'main'})
    store.create({'issue_url': 'https://github.com/acme/two/issues/2', 'base_branch': 'main'})
    monkeypatch.setattr(app_module, 'store', store)

    with app_module.app.test_client() as client:
        response = client.post('/settings/clear-queue')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/settings?queue_cleared=2')
    assert store.list() == []
    assert store.get(first) is None


def test_settings_demo_notification_opens_demo_ticket(monkeypatch):
    sent = {}

    def fake_notification(title, body, log, launch_url=None):
        sent.update({
            'title': title,
            'body': body,
            'launch_url': launch_url,
        })
        log('Windows notification sent.')

    monkeypatch.setattr(app_module.os, 'name', 'nt')
    monkeypatch.setattr(app_module.shutil, 'which', lambda name: name)
    monkeypatch.setattr(app_module.WorkflowRunner, '_send_windows_notification', fake_notification)

    with app_module.app.test_client() as client:
        response = client.post('/settings/demo-notification')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/settings?notification=sent')
    assert sent['launch_url'] == 'https://github.com/pvfscaffolding/crm-staff-desktop/issues/898'
    assert 'ticket #898' in sent['body']


def test_dashboard_exposes_claude_ladder_launcher():
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/jobs')

    assert response.status_code == 200
    assert b'Run Claude Ladder' in response.data
    assert b'Haiku -> Sonnet -> Luna -> Opus -> Sol Low -> Sol High' in response.data
    assert b"launchClaude(this.form, 'claude-haiku-4-5')" in response.data
    assert b'claude-haiku-4-5' in response.data
    assert b'function withClaudeModel(command, model)' in response.data
    assert b'const command = withClaudeModel(base, model)' in response.data


def test_dashboard_exposes_chatgpt_55_instant_low_launcher():
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/jobs')

    assert response.status_code == 200
    assert b'Run Fast OpenAI' in response.data
    assert b'Luna -> Opus -> Sol Low -> Sol High' in response.data
    assert b'function launchInstant(form)' in response.data
    assert b'/home/claytongatting/.npm-global/bin/codex exec' in response.data
    assert b'-c \'model="gpt-5.6-luna"\'' in response.data
    assert b'-c \'model_reasoning_effort="low"\'' in response.data
    assert b'--skip-git-repo-check --json -' in response.data


def test_dashboard_exposes_three_distinct_model_pipelines():
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/jobs')

    assert response.status_code == 200
    assert b'Run Fast OpenAI' in response.data
    assert b'Run Claude Ladder' in response.data
    assert b'OPENAI // MAXIMUM' in response.data
    assert b'Deploy Sonnet' not in response.data
    assert b'Deploy Opus' not in response.data
    assert b'function launchUltra(form)' not in response.data


def test_dashboard_shows_codex_update_loading_page_when_startup_check_is_running(monkeypatch):
    app_module.app.config.update(TESTING=True, CODEX_UPDATE_ON_OPEN=True)
    monkeypatch.setattr(app_module, '_ensure_codex_update_started', lambda: {
        'status': 'running',
        'message': 'Applying Codex CLI update if one is available...',
        'before_version': 'codex 1.0.0',
        'after_version': '',
    })

    try:
        response = app_module.app.test_client().get('/jobs')

        assert response.status_code == 200
        assert b'Checking Codex CLI' in response.data
        assert b'/api/codex-update' in response.data
        assert b'Applying Codex CLI update if one is available...' in response.data
    finally:
        app_module.app.config.update(CODEX_UPDATE_ON_OPEN=False)


def test_testing_workspace_locks_evidence_review_to_fixed_pipeline():
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get('/testing')

    assert response.status_code == 200
    assert b'id="testing-provider"' not in response.data
    assert b'id="testing-pipeline-gauge"' in response.data
    for tier in (b'Haiku', b'Sonnet', b'Luna', b'Opus', b'Sol Low', b'Sol High'):
        assert tier in response.data
    assert b'<option value="qwen">' not in response.data


def test_selecting_wwii_sets_durable_cookie_and_applies_global_skin():
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        response = client.post('/settings/theme', data={'theme': 'wwii'})

        assert response.status_code == 302
        assert response.headers['Location'].endswith('/settings?saved=wwii')
        cookie = response.headers['Set-Cookie']
        assert 'mergequest_theme=wwii' in cookie
        assert 'Max-Age=31536000' in cookie
        assert 'HttpOnly' in cookie
        assert 'SameSite=Lax' in cookie
        assert 'Path=/' in cookie

        settings_response = client.get('/settings')
        prompts_response = client.get('/prompts')

    assert b'<html lang="en" data-theme="wwii">' in settings_response.data
    assert b'name="theme" value="wwii" checked' in settings_response.data
    assert b'theme-wwii.css' in settings_response.data
    assert b'WAR ROOM // FIELD HQ 1944' in settings_response.data
    assert b'<html lang="en" data-theme="wwii">' in prompts_response.data
    assert b'Operations codebook' in prompts_response.data
    assert b'Radio GitHub command' in prompts_response.data


@pytest.mark.parametrize(
    ('theme', 'stylesheet', 'brand_marker', 'settings_marker', 'page_marker'),
    [
        ('hacker', b'theme-hacker.css', b'ROOT SHELL // SECURE CONSOLE', b'ROOT TERMINAL // CONFIG', b'Payload laboratory'),
        ('skyrim', b'theme-skyrim.css', b'NORDIC SAGA // QUEST LEDGER', b'STEWARD&#39;S TABLE // GREAT HALL', b'Arcane codex'),
        ('professional', b'theme-professional.css', b'WORKSTATION // OPERATIONS HUB', b'WORKSPACE ADMIN // CONFIGURATION', b'Prompt studio'),
    ],
)
def test_selecting_additional_theme_sets_durable_cookie_and_global_skin(
    theme: str,
    stylesheet: bytes,
    brand_marker: bytes,
    settings_marker: bytes,
    page_marker: bytes,
):
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        response = client.post('/settings/theme', data={'theme': theme})

        assert response.status_code == 302
        assert response.headers['Location'].endswith(f'/settings?saved={theme}')
        cookie = response.headers['Set-Cookie']
        assert f'mergequest_theme={theme}' in cookie
        assert 'Max-Age=31536000' in cookie
        assert 'HttpOnly' in cookie
        assert 'SameSite=Lax' in cookie
        assert 'Path=/' in cookie

        settings_response = client.get('/settings')
        prompts_response = client.get('/prompts')

    selected_input = f'name="theme" value="{theme}" checked'.encode()
    theme_attribute = f'<html lang="en" data-theme="{theme}">'.encode()
    assert theme_attribute in settings_response.data
    assert selected_input in settings_response.data
    assert stylesheet in settings_response.data
    assert brand_marker in settings_response.data
    assert settings_marker in settings_response.data
    assert theme_attribute in prompts_response.data
    assert stylesheet in prompts_response.data
    assert brand_marker in prompts_response.data
    assert page_marker in prompts_response.data


def test_theme_can_switch_back_to_cyberpunk():
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        client.post('/settings/theme', data={'theme': 'wwii'})
        response = client.post(
            '/settings/theme', data={'theme': 'cyberpunk'}, follow_redirects=True,
        )

    assert response.status_code == 200
    assert b'<html lang="en" data-theme="cyberpunk">' in response.data
    assert b'name="theme" value="cyberpunk" checked' in response.data
    assert b'theme-wwii.css' not in response.data
    assert b'theme-hacker.css' not in response.data
    assert b'theme-skyrim.css' not in response.data


def test_invalid_theme_is_rejected_without_reflecting_or_overwriting_choice():
    app_module.app.config.update(TESTING=True)
    attack = '<script>alert(1)</script>'
    with app_module.app.test_client() as client:
        client.post('/settings/theme', data={'theme': 'wwii'})
        response = client.post('/settings/theme', data={'theme': attack})
        removed_response = client.post('/settings/theme', data={'theme': 'wasteland'})
        follow_up = client.get('/settings')

    assert response.status_code == 400
    assert removed_response.status_code == 400
    assert 'Set-Cookie' not in response.headers
    assert 'Set-Cookie' not in removed_response.headers
    assert attack.encode() not in response.data
    assert b'Choose one of the available interface themes.' in response.data
    assert b'name="theme" value="wasteland"' not in follow_up.data
    assert b'theme-wasteland.css' not in follow_up.data
    assert b'data-theme="wwii"' in response.data
    assert b'data-theme="wwii"' in follow_up.data


def test_theme_preference_is_isolated_per_browser_and_survives_logout():
    app_module.app.config.update(TESTING=True)
    themed_client = app_module.app.test_client()
    default_client = app_module.app.test_client()

    themed_client.post('/settings/theme', data={'theme': 'wwii'})
    themed_client.get('/logout')

    themed_response = themed_client.get('/prompts')
    default_response = default_client.get('/prompts')

    assert b'data-theme="wwii"' in themed_response.data
    assert b'data-theme="cyberpunk"' in default_response.data


@pytest.mark.parametrize(
    ('theme', 'stylesheet', 'brand_marker', 'sync_marker'),
    [
        ('cyberpunk', None, b'NEON OPS // BUILD 2.077', b'AUTONOMOUS SEQUENCE'),
        ('wwii', b'theme-wwii.css', b'WAR ROOM // FIELD HQ 1944', b'COORDINATED SEQUENCE'),
        ('hacker', b'theme-hacker.css', b'ROOT SHELL // SECURE CONSOLE', b'FORKED PROCESS CHAIN'),
        ('skyrim', b'theme-skyrim.css', b'NORDIC SAGA // QUEST LEDGER', b'COURIER RITUAL'),
        ('professional', b'theme-professional.css', b'WORKSTATION // OPERATIONS HUB', b'WORKSPACE SYNC'),
    ],
)
def test_each_theme_renders_every_primary_page(
    monkeypatch,
    theme: str,
    stylesheet: bytes | None,
    brand_marker: bytes,
    sync_marker: bytes,
):
    app_module.app.config.update(TESTING=True)
    fake_job = {
        'id': 'theme-smoke',
        'parameters': {'workflow_profile': 'full_pr'},
        'stage': 'Checking GitHub access',
        'status': 'queued',
        'issue_url': 'https://github.com/example/project/issues/7',
        'base_branch': 'develop',
        'created_at': '2026-07-18T10:00:00+00:00',
        'updated_at': '2026-07-18T10:00:00+00:00',
        'approval_message': '',
        'error': None,
        'result': None,
        'logs': '',
    }
    monkeypatch.setattr(app_module.store, 'list', lambda limit=500: [])
    monkeypatch.setattr(app_module.store, 'list_tickets', lambda *args, **kwargs: [])
    monkeypatch.setattr(app_module.store, 'list_testing_tickets', lambda: [])
    monkeypatch.setattr(app_module.store, 'list_ticket_repositories', lambda: [])
    monkeypatch.setattr(app_module.store, 'list_ticket_references', lambda: [])
    monkeypatch.setattr(app_module.store, 'leaderboard', lambda: [])
    monkeypatch.setattr(app_module.store, 'get', lambda job_id: fake_job)

    paths = [
        '/jobs',
        '/prompts',
        '/testing',
        '/leaderboard',
        '/settings',
        '/jobs/theme-smoke',
    ]
    with app_module.app.test_client() as client:
        selection = client.post('/settings/theme', data={'theme': theme})
        assert selection.status_code == 302
        responses = {path: client.get(path) for path in paths}

    alternate_stylesheets = [b'theme-wwii.css', b'theme-hacker.css', b'theme-skyrim.css', b'theme-professional.css']
    theme_attribute = f'data-theme="{theme}"'.encode()
    for path, response in responses.items():
        assert response.status_code == 200, path
        assert theme_attribute in response.data, path
        assert brand_marker in response.data, path
        if path == '/jobs':
            assert sync_marker in response.data
        if stylesheet is None:
            assert all(candidate not in response.data for candidate in alternate_stylesheets), path
        else:
            assert stylesheet in response.data, path
