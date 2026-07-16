from __future__ import annotations

import app as app_module


def test_ticket_sync_reports_authentication_failure_as_401(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        'sync_github',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError('GitHub CLI did not provide an authentication token.')
        ),
    )
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        '/tickets/sync',
        json={'repository': 'org/repo'},
    )

    assert response.status_code == 401
    assert response.get_json()['kind'] == 'authentication'


def test_ticket_sync_reports_invalid_repository_as_422(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        'sync_github',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError('Repository must use the `owner/repository` format.')
        ),
    )
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        '/tickets/sync',
        json={'repository': 'https://github.com/org/repo'},
    )

    assert response.status_code == 422
    assert response.get_json()['kind'] == 'validation'
