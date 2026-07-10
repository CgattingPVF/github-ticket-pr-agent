from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook


def _text(value: object) -> str:
    return '' if value is None else str(value).strip()


def _priority(value: object) -> str:
    value = _text(value).upper()
    return value if value in {'P0', 'P1', 'P2'} else ''


def import_workbook(path: Path) -> list[dict]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [_text(value) for value in rows[0]]
    index = {name: i for i, name in enumerate(headers) if name}
    required = {'Issue_URL', 'Issue_Number', 'Issue_Title'}
    missing = required - index.keys()
    if missing:
        raise ValueError(f'Workbook is missing columns: {", ".join(sorted(missing))}')
    now = datetime.now(timezone.utc).isoformat()
    tickets = []
    for row in rows[1:]:
        get = lambda name: _text(row[index[name]]) if index[name] < len(row) else ''
        url = get('Issue_URL')
        if not url or '/issues/' not in url:
            continue
        repository = get('PF__Repository')
        if not repository:
            parts = url.split('/')
            repository = '/'.join(parts[3:5]) if len(parts) > 4 else ''
        number = int(float(get('Issue_Number'))) if get('Issue_Number') else 0
        tickets.append({'key': f'{repository}#{number}', 'repository': repository, 'number': number,
                        'url': url, 'title': get('Issue_Title'), 'state': get('Issue_State').upper() or 'OPEN',
                        'labels': get('Issue_Labels'), 'assignees': get('Issue_Assignees'),
                        'priority': _priority(get('PF__Project Priority')), 'project_status': get('PF__Status'),
                        'issue_type': get('Issue_Type'), 'created_at': get('Created_At'),
                        'updated_at': get('Updated_At'), 'synced_at': now, 'source': 'xlsx'})
    return tickets


def sync_github(repository: str = '', state: str = 'open', limit: int = 100) -> list[dict]:
    endpoint = f'repos/{repository}/issues?state={state}&per_page={min(limit, 100)}' if repository else f'search/issues?q=state:{state}+is:issue&per_page={min(limit, 100)}'
    result = subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'GitHub API request failed')
    payload = json.loads(result.stdout)
    items = payload.get('items', []) if isinstance(payload, dict) else payload
    now = datetime.now(timezone.utc).isoformat()
    tickets = []
    for issue in items:
        repo = issue.get('repository', {}).get('full_name', repository) if isinstance(issue, dict) else repository
        labels = ', '.join(label.get('name', '') for label in issue.get('labels', []))
        label_names = {label.get('name', '').upper() for label in issue.get('labels', [])}
        priority = next((value for value in ('P0', 'P1', 'P2') if value in label_names), '')
        assignees = ', '.join(person.get('login', '') for person in issue.get('assignees', []))
        tickets.append({'key': f'{repo}#{issue["number"]}', 'repository': repo, 'number': issue['number'],
                        'url': issue.get('html_url', ''), 'title': issue.get('title', ''),
                        'state': issue.get('state', 'open').upper(), 'labels': labels, 'assignees': assignees,
                        'priority': priority, 'project_status': '', 'issue_type': 'Issue',
                        'created_at': issue.get('created_at', ''), 'updated_at': issue.get('updated_at', ''),
                        'synced_at': now, 'source': 'github'})
    return tickets
