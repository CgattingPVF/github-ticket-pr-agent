from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request, redirect, url_for, session
from core import generate_test_plan, make_branch_name, parse_issue_url
from config import Settings
from prompts import all_in_one_prompt, investigation_prompt, review_prompt
from store import JobStore
from ticket_sync import find_gh_executable, import_workbook, sync_github
from workflow import WorkflowRunner
from github_ops import GitHubOps


def get_github_token():
    """Get GitHub token from session or environment."""
    return session.get('github_token') or os.getenv('GH_TOKEN')


def find_available_port(start_port: int = 3060, max_attempts: int = 100) -> int:
    for port in range(start_port, start_port + max_attempts):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", port))
            sock.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"No available ports found between {start_port} and {start_port + max_attempts}")


def fetch_issue_from_github(issue_url: str) -> dict:
    repo, issue_num = parse_github_url(issue_url)
    try:
        env = os.environ.copy()
        token = get_github_token()
        if token:
            env['GH_TOKEN'] = token
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_num}"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            raise Exception(f"GitHub API error: {stderr or 'Unknown error'}")
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise Exception("GitHub API request timed out")
    except json.JSONDecodeError as e:
        raise Exception(f"Invalid response from GitHub: {e}")
    except Exception as e:
        raise Exception(f"Failed to fetch issue: {e}")


def post_issue_comment(issue_url: str, body: str) -> None:
    repo, issue_num = parse_github_url(issue_url)
    env = os.environ.copy()
    token = get_github_token()
    if token:
        env['GH_TOKEN'] = token
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{issue_num}/comments", "-f", f"body={body}"],
        capture_output=True, text=True, timeout=20, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Unable to comment on the GitHub ticket")


def parse_github_url(url: str) -> tuple[str, str]:
    """Parse github.com/owner/repo/issues/number -> (owner/repo, number)"""
    url = url.strip().rstrip("/")
    if "github.com" not in url:
        raise ValueError(f"Not a GitHub URL: {url}")

    parts = url.split("/")
    try:
        issues_idx = parts.index("issues")
        owner = parts[issues_idx - 2]
        repo = parts[issues_idx - 1]
        number = parts[issues_idx + 1]
        return f"{owner}/{repo}", number
    except (ValueError, IndexError):
        raise ValueError(f"Invalid GitHub issue URL format: {url}")

def fetch_repository_prs(repository: str) -> list[dict]:
    token = get_github_token()
    result = subprocess.run(['gh', 'api', f'repos/{repository}/pulls?state=all&per_page=100&sort=updated&direction=desc'], capture_output=True, text=True, timeout=20, env={**os.environ, **({'GH_TOKEN': token} if token else {})})
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'Unable to read pull requests for {repository}')
    now = datetime.now(timezone.utc)
    prs = []
    for pr in json.loads(result.stdout):
        created = datetime.fromisoformat(pr['created_at'].replace('Z', '+00:00'))
        end = pr.get('merged_at') or pr.get('closed_at')
        finished = datetime.fromisoformat(end.replace('Z', '+00:00')) if end else now
        seconds = max(0, int((finished - created).total_seconds()))
        prs.append({**pr, 'repository': repository, 'cycle_seconds': seconds, 'cycle_time': f'{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}', 'state_label': 'merged' if pr.get('merged_at') else pr.get('state', 'open')})
    return prs


# Street-cred tiers set the operator title and passive contract-network yield.
STREET_CRED_TIERS = [
    {'rank': 'Back-Alley Runner', 'code': 'SC-01', 'rate': 5},
    {'rank': 'Chrome Rookie', 'code': 'SC-02', 'rate': 12},
    {'rank': 'Ghost Operator', 'code': 'SC-03', 'rate': 25},
    {'rank': 'Netrunner', 'code': 'SC-04', 'rate': 45},
    {'rank': 'Blackwall Specialist', 'code': 'SC-05', 'rate': 80},
    {'rank': 'Afterlife Merc', 'code': 'SC-06', 'rate': 140},
    {'rank': 'Night Legend', 'code': 'SC-07', 'rate': 240},
    {'rank': 'City Icon', 'code': 'SC-08', 'rate': 400},
]
XP_PER_LEVEL = 400
XP_COMPLETED = 120
XP_INTEL = 20


def player_stats(jobs: list[dict]) -> dict:
    """Derive Neon Ops street cred, credits, streaks, and accolades."""
    completed = [j for j in jobs if j['status'] == 'completed']
    failed = [j for j in jobs if j['status'] == 'failed']
    xp = len(completed) * XP_COMPLETED + len(failed) * XP_INTEL
    level = 1 + xp // XP_PER_LEVEL
    tier = STREET_CRED_TIERS[min(level - 1, len(STREET_CRED_TIERS) - 1)]
    next_rank = STREET_CRED_TIERS[level] if level < len(STREET_CRED_TIERS) else None
    rank = tier['rank']

    # Every cleared contract becomes an income-producing network asset.
    now = datetime.now(timezone.utc)
    banked = 0.0
    for j in completed:
        ts = j.get('updated_at')
        if not ts:
            continue
        try:
            merged = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            if merged.tzinfo is None:
                merged = merged.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        hours = max(0.0, (now - merged).total_seconds() / 3600)
        banked += tier['rate'] * hours
    network_assets = len(completed)
    credits_rate = network_assets * tier['rate']
    # A quarter of passive yield is automatically reinvested in Blackwall
    # maintenance: the currency has a visible purpose beyond a counter.
    maintenance_fund = round(banked * 0.25)
    maintenance_target = 1000

    days = {j['updated_at'][:10] for j in completed if j.get('updated_at')}
    streak, day = 0, datetime.now(timezone.utc).date()
    if day.isoformat() not in days:
        day -= timedelta(days=1)  # a streak survives until the end of today
    while day.isoformat() in days:
        streak += 1
        day -= timedelta(days=1)

    def flawless(job):
        review = (job.get('result') or {}).get('review') or {}
        return not review.get('findings')

    comeback = any(
        c['created_at'] > f['created_at'] for c in completed for f in failed
    )
    achievements = [
        {'icon': '01', 'name': 'First Blood', 'desc': 'Clear your first contract', 'unlocked': len(completed) >= 1},
        {'icon': '03', 'name': 'Triple Breach', 'desc': 'Clear 3 contracts', 'unlocked': len(completed) >= 3},
        {'icon': '10', 'name': 'Daemon Hunter', 'desc': 'Clear 10 contracts', 'unlocked': len(completed) >= 10},
        {'icon': '00', 'name': 'Zero Trace', 'desc': 'Deploy with zero review findings', 'unlocked': any(flawless(j) for j in completed)},
        {'icon': '3D', 'name': 'Overclocked', 'desc': 'Clear contracts 3 days in a row', 'unlocked': streak >= 3},
        {'icon': '5X', 'name': 'Data Scavenger', 'desc': 'Extract intel from 5 interrupted contracts', 'unlocked': len(failed) >= 5},
        {'icon': 'RX', 'name': 'Second Heart', 'desc': 'Clear a contract after a failed breach', 'unlocked': comeback},
    ]
    return {
        'xp': xp,
        'level': level,
        'rank': rank,
        'rank_code': tier['code'],
        'next_rank': next_rank,
        'network_assets': network_assets,
        'credits_rate': credits_rate,
        'credits_banked': round(banked),
        'maintenance_fund': maintenance_fund,
        'maintenance_target': maintenance_target,
        'maintenance_progress_pct': min(100, round(maintenance_fund / maintenance_target * 100)),
        'streak': streak,
        'xp_into_level': xp % XP_PER_LEVEL,
        'xp_per_level': XP_PER_LEVEL,
        'progress_pct': round((xp % XP_PER_LEVEL) / XP_PER_LEVEL * 100),
        'completed': len(completed),
        'failed': len(failed),
        'achievements': achievements,
        'unlocked_count': sum(1 for a in achievements if a['unlocked']),
    }


app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-only-change-me')
settings = Settings()
settings.ensure_directories()
store = JobStore(settings.database_path)
runner = WorkflowRunner(settings, store)

oauth = OAuth(app)
github = oauth.register(
    name='github',
    client_id=os.getenv('GITHUB_CLIENT_ID', ''),
    client_secret=os.getenv('GITHUB_CLIENT_SECRET', ''),
    access_token_url='https://github.com/login/oauth/access_token',
    access_token_params=None,
    authorize_url='https://github.com/login/oauth/authorize',
    authorize_params=None,
    api_base_url='https://api.github.com/',
    client_kwargs={'scope': 'repo public_repo'},
)

_cli_auth_lock = threading.Lock()
_cli_auth_state: dict[str, object] = {
    'status': 'idle',
    'message': 'Ready to connect to GitHub.',
    'output': [],
    'started_at': None,
}


def _subprocess_window_flags() -> int:
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


def _github_cli_identity() -> dict | None:
    """Return the active GitHub CLI identity without exposing its token."""
    try:
        gh = find_gh_executable()
        token_result = subprocess.run(
            [gh, 'auth', 'token', '--hostname', 'github.com'],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_subprocess_window_flags(),
        )
        token = token_result.stdout.strip()
        if token_result.returncode != 0 or not token:
            return None
        profile = subprocess.run(
            [gh, 'api', 'user'],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'GH_TOKEN': token},
            creationflags=_subprocess_window_flags(),
        )
        if profile.returncode != 0:
            return None
        return json.loads(profile.stdout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _connect_github_cli_session() -> dict | None:
    user = _github_cli_identity()
    if not user or not user.get('login'):
        return None
    session['github_login'] = user['login']
    store.upsert_player(
        user['login'],
        user.get('name') or user['login'],
        user.get('avatar_url', ''),
    )
    return user


def _run_cli_authentication() -> None:
    try:
        gh = find_gh_executable()
        env = os.environ.copy()
        # Force an interactive persisted login instead of reusing an invalid
        # automation token inherited from the process environment.
        env.pop('GH_TOKEN', None)
        env.pop('GITHUB_TOKEN', None)
        process = subprocess.Popen(
            [
                gh, 'auth', 'login',
                '--hostname', 'github.com',
                '--git-protocol', 'https',
                '--web',
                '--scopes', 'repo,read:org,project',
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            creationflags=_subprocess_window_flags(),
        )
        with _cli_auth_lock:
            _cli_auth_state['status'] = 'waiting'
            _cli_auth_state['message'] = 'Complete authorization in the GitHub browser tab.'

        output: list[str] = []
        if process.stdout:
            for line in process.stdout:
                clean_line = line.strip()
                if clean_line:
                    output.append(clean_line)
                    with _cli_auth_lock:
                        _cli_auth_state['output'] = output[-12:]

        return_code = process.wait()
        with _cli_auth_lock:
            if return_code == 0:
                _cli_auth_state['status'] = 'complete'
                _cli_auth_state['message'] = 'GitHub authorization completed.'
            else:
                _cli_auth_state['status'] = 'failed'
                _cli_auth_state['message'] = output[-1] if output else 'GitHub CLI sign-in failed.'
    except Exception as exc:
        with _cli_auth_lock:
            _cli_auth_state['status'] = 'failed'
            _cli_auth_state['message'] = f'Unable to start GitHub sign-in: {exc}'


def _start_cli_authentication() -> dict[str, object]:
    with _cli_auth_lock:
        if _cli_auth_state['status'] in {'starting', 'waiting'}:
            return dict(_cli_auth_state)
        _cli_auth_state.update({
            'status': 'starting',
            'message': 'Opening GitHub authorization…',
            'output': [],
            'started_at': time.time(),
        })
    threading.Thread(target=_run_cli_authentication, daemon=True).start()
    with _cli_auth_lock:
        return dict(_cli_auth_state)


@app.get('/login')
def login():
    client_id = os.getenv('GITHUB_CLIENT_ID', '')
    # Local development commonly has GitHub CLI authentication but no OAuth app.
    # Reuse that identity instead of sending a placeholder client id to GitHub.
    if not client_id or client_id in {'your-client-id-here', 'change-me'}:
        if _connect_github_cli_session():
            return redirect(url_for('prompts_page'))
        return render_template('github_login.html')
    if client_id:
        return github.authorize_redirect(url_for('auth_callback', _external=True))
    return redirect(url_for('prompts_page'))


@app.post('/auth/cli/start')
def start_cli_auth():
    if _connect_github_cli_session():
        return jsonify({'status': 'connected', 'redirect': url_for('prompts_page')})
    return jsonify(_start_cli_authentication())


@app.get('/auth/cli/status')
def cli_auth_status():
    user = _connect_github_cli_session()
    if user:
        return jsonify({
            'status': 'connected',
            'login': user['login'],
            'redirect': url_for('prompts_page'),
        })
    with _cli_auth_lock:
        return jsonify(dict(_cli_auth_state))


@app.get('/auth/callback')
def auth_callback():
    try:
        token = github.authorize_access_token()
        session['github_token'] = token.get('access_token')
        token_value = token.get('access_token')
        profile = subprocess.run(['gh', 'api', 'user'], capture_output=True, text=True, timeout=10, env={**os.environ, 'GH_TOKEN': token_value})
        if profile.returncode == 0:
            user = json.loads(profile.stdout)
            session['github_login'] = user.get('login')
            store.upsert_player(user.get('login', ''), user.get('name') or user.get('login', ''), user.get('avatar_url', ''))
        return redirect(url_for('prompts_page'))
    except Exception:
        return redirect(url_for('prompts_page'))


@app.get('/logout')
def logout():
    session.pop('github_token', None)
    session.pop('github_login', None)
    return redirect(url_for('prompts_page'))


@app.get('/api/user')
def api_user():
    if session.get('github_login'):
        return jsonify({'login': session['github_login']})
    token = get_github_token()
    if not token:
        return jsonify({'login': None}), 401
    try:
        result = subprocess.run(
            ["gh", "api", "user"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'GH_TOKEN': token}
        )
        if result.returncode == 0:
            return jsonify(json.loads(result.stdout))
        return jsonify({'login': None}), 401
    except Exception:
        return jsonify({'login': None}), 401


@app.get('/system/gh')
def github_cli_status():
    try:
        executable = Path(find_gh_executable())
        return jsonify({
            'available': True,
            'bundled': bool(getattr(sys, '_MEIPASS', None)) and executable.parent == Path(sys._MEIPASS),
        })
    except RuntimeError as exc:
        return jsonify({'available': False, 'bundled': False, 'error': str(exc)}), 503


@app.get('/jobs')
def index():
    jobs = store.list(limit=500)
    watch_ids = [item for item in request.args.get('watch', '').split(',') if item]
    watched_jobs = [store.get(job_id) for job_id in watch_ids]
    watched_jobs = [job for job in watched_jobs if job]
    return render_template('index.html', jobs=jobs, tickets=store.list_tickets(), defaults=settings, player=player_stats(jobs), watched_jobs=watched_jobs)

@app.get('/leaderboard')
def leaderboard_page():
    jobs = store.list(limit=500)
    current = player_stats([j for j in jobs if j.get('github_login') == session.get('github_login')])
    repositories = sorted({parse_github_url(j['issue_url'])[0] for j in jobs if j.get('issue_url')})
    repository = request.args.get('repository') or (repositories[0] if repositories else '')
    prs, prs_error = [], None
    if repository:
        try: prs = fetch_repository_prs(repository)
        except Exception as exc: prs_error = str(exc)
    authors = {}
    for pr in prs:
        author = pr.get('user', {}).get('login', 'unknown')
        item = authors.setdefault(author, {'author': author, 'prs': 0, 'merged': 0, 'open': 0, 'closed': 0, 'additions': 0, 'deletions': 0, 'total_seconds': 0})
        item['prs'] += 1; item[pr['state_label']] = item.get(pr['state_label'], 0) + 1
        item['merged'] += 1 if pr['state_label'] == 'merged' else 0
        item['additions'] += pr.get('additions', 0); item['deletions'] += pr.get('deletions', 0); item['total_seconds'] += pr['cycle_seconds']
    author_stats = sorted(authors.values(), key=lambda x: (-x['merged'], -x['prs'], x['total_seconds']))
    fastest_prs = sorted(prs, key=lambda x: x['cycle_seconds'])[:10]
    stats = {'total': len(prs), 'merged': sum(p['state_label'] == 'merged' for p in prs), 'open': sum(p['state_label'] == 'open' for p in prs), 'closed': sum(p['state_label'] == 'closed' for p in prs), 'additions': sum(p.get('additions', 0) for p in prs), 'deletions': sum(p.get('deletions', 0) for p in prs)}
    return render_template('leaderboard.html', leaderboard=store.leaderboard(), player=current, github_login=session.get('github_login'), repositories=repositories, repository=repository, prs=prs, author_stats=author_stats, fastest_prs=fastest_prs, stats=stats, prs_error=prs_error)

@app.get('/settings')
def settings_page():
    return render_template('settings.html', settings=settings)


@app.post('/jobs')
def create_job():
    form = request.form
    issue_urls = [url.strip() for url in form.getlist('issue_url') if url.strip()]
    if not issue_urls:
        issue_urls = [form.get('issue_url', '').strip()]
    issue_urls = list(dict.fromkeys(issue_urls))
    if len(issue_urls) > 3:
        return jsonify({'error': 'Select no more than 3 tickets at a time'}), 400
    parameters = {
        'issue_url': issue_urls[0] if issue_urls else '',
        'base_branch': form.get('base_branch', 'develop').strip(),
        'branch_prefix': form.get('branch_prefix', 'bug-fix').strip(),
        'agent_command': form.get('agent_command', '').strip(),
        'review_command': form.get('review_command', '').strip(),
        'agent_provider': form.get('agent_provider', 'codex'),
        'review_provider': form.get('review_provider', 'codex'),
        'validation_commands': form.get('validation_commands', ''),
        'close_issue_on_merge': form.get('close_issue_on_merge') == 'on',
        'comment_on_failure': form.get('comment_on_failure') == 'on',
        'approval_mode': 'each_stage' if form.get('workflow_profile') == 'manual' else form.get('approval_mode', 'auto'),
        'workflow_profile': form.get('workflow_profile', 'full_pr'),
        'github_login': session.get('github_login'),
    }
    if not parameters['issue_url']:
        jobs = store.list(limit=500)
        return render_template('index.html', jobs=jobs, tickets=store.list_tickets(), defaults=settings, player=player_stats(jobs), form=form, form_error='Issue URL required'), 400
    job_ids = []
    for issue_url in issue_urls:
        job_parameters = {**parameters, 'issue_url': issue_url}
        job_id = store.create(job_parameters)
        runner.start(job_id)
        job_ids.append(job_id)
    if len(job_ids) > 1:
        return redirect(url_for('index', watch=','.join(job_ids)))
    return redirect(url_for('job_detail', job_id=job_ids[0]))


@app.get('/jobs/<job_id>')
def job_detail(job_id):
    job = store.get(job_id)
    if not job:
        return 'Job not found', 404
    return render_template('job.html', job=job)


@app.get('/api/jobs/<job_id>')
def api_job(job_id):
    job = store.get(job_id)
    return (jsonify(job), 200) if job else (jsonify({'error': 'Job not found'}), 404)


@app.post('/api/jobs/<job_id>/approval')
def job_approval(job_id):
    action = request.json.get('action', '')
    if action not in {'approve', 'reject'}:
        return jsonify({'error': 'action must be approve or reject'}), 400
    if not store.get(job_id):
        return jsonify({'error': 'Job not found'}), 404
    store.update(job_id, approval_state='approved' if action == 'approve' else 'rejected')
    return jsonify(store.get(job_id))


@app.post('/api/jobs/<job_id>/cancel')
def cancel_job(job_id):
    if not store.get(job_id):
        return jsonify({'error': 'Job not found'}), 404
    store.update(job_id, status='cancelled', approval_state='rejected', error='Cancelled by user')
    return jsonify(store.get(job_id))


@app.post('/api/jobs/<job_id>/stop')
def stop_job(job_id):
    job = store.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] in {'completed', 'failed', 'cancelled', 'stopped', 'closed'}:
        return jsonify({'error': 'Job is already finished'}), 409
    store.stop(job_id)
    return jsonify(store.get(job_id))


@app.post('/api/jobs/<job_id>/continue-pr')
def continue_to_pr(job_id):
    job = store.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'completed' or not (job['result'] or {}).get('pr_skipped'):
        return jsonify({'error': 'Job is not a completed investigate & fix run'}), 409
    runner.continue_to_pr(job_id)
    return jsonify(store.get(job_id))


@app.post('/api/jobs/<job_id>/close')
def close_job(job_id):
    job = store.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] in {'queued', 'running', 'waiting_approval'}:
        return jsonify({'error': 'Stop the job before closing it'}), 409
    store.update(job_id, status='closed')
    store.append_log(job_id, 'Job closed by user.')
    return jsonify(store.get(job_id))


@app.get("/")
def root():
    return redirect(url_for("index"))


@app.get("/prompts")
def prompts_page():
    return render_template('prompts.html')


@app.get("/testing")
def testing_page():
    return render_template('testing.html')


@app.post("/testing/plan")
def testing_plan():
    issue_url = (request.get_json(silent=True) or {}).get('issue_url', '').strip()
    if not issue_url:
        return jsonify({'error': 'GitHub issue URL required'}), 400
    try:
        repo, issue_num = parse_github_url(issue_url)
        issue = fetch_issue_from_github(issue_url)
        if issue.get('pull_request'):
            return jsonify({'error': 'Enter an issue ticket URL, not a pull request'}), 400
        job = store.latest_for_issue(issue_url)
        checks = (job.get('result') or {}).get('tests_run', []) if job else []
        return jsonify({
            'issue': {'url': issue_url, 'number': issue.get('number'), 'title': issue.get('title', ''), 'repository': repo},
            'job': ({'id': job['id'], 'status': job['status'], 'stage': job['stage'], 'updated_at': job['updated_at']} if job else None),
            'checks': checks,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.post("/testing/run")
def testing_run():
    payload = request.get_json(silent=True) or {}
    issue_url = str(payload.get('issue_url', '')).strip()
    provider = str(payload.get('provider', 'codex')).lower()
    if not issue_url:
        return jsonify({'error': 'GitHub issue URL required'}), 400
    if provider not in {'codex', 'claude'}:
        return jsonify({'error': 'Provider must be codex or claude'}), 400
    try:
        parse_github_url(issue_url)
        job_id = store.create({
            'issue_url': issue_url, 'base_branch': 'develop',
            'workflow_profile': 'testing_only', 'agent_provider': provider,
            'agent_command': '', 'approval_mode': 'auto',
            'github_login': session.get('github_login'),
        })
        runner.start_testing(job_id)
        return jsonify({'job_id': job_id}), 202
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.post("/testing/jobs/<job_id>/fix")
def testing_fix(job_id: str):
    source = store.get(job_id)
    if not source or source.get('parameters', {}).get('workflow_profile') != 'testing_only':
        return jsonify({'error': 'Failed QA run not found'}), 404
    result = source.get('result') or {}
    if str(result.get('overall', '')).lower() == 'passed':
        return jsonify({'error': 'This QA run already passed'}), 400
    provider = str((request.get_json(silent=True) or {}).get('provider') or result.get('provider') or 'codex').lower()
    fix_id = store.create({
        'issue_url': source['issue_url'], 'base_branch': 'develop',
        'workflow_profile': 'qa_fix', 'agent_provider': provider,
        'review_provider': provider, 'agent_command': '', 'review_command': '',
        'branch_prefix': 'bug-fix', 'validation_commands': '',
        'close_issue_on_merge': False, 'comment_on_failure': True,
        'approval_mode': 'auto',
        'github_login': session.get('github_login'),
    })
    # Route through the same evidence, validation, review, repair, and publish
    # pipeline as the Autonomous Daemon. The qa_fix profile changes only branch
    # preparation and the final PR action.
    runner.start(fix_id)
    return jsonify({'job_id': fix_id}), 202


@app.post("/testing/jobs/<job_id>/override")
def testing_override(job_id: str):
    source = store.get(job_id)
    if not source or source.get('parameters', {}).get('workflow_profile') != 'testing_only':
        return jsonify({'error': 'Completed autonomous QA run not found'}), 404
    if source.get('status') != 'completed':
        return jsonify({'error': 'QA outcomes can only be overridden after the run completes'}), 400

    payload = request.get_json(silent=True) or {}
    try:
        index = int(payload.get('index'))
    except (TypeError, ValueError):
        return jsonify({'error': 'A valid test index is required'}), 400
    status = str(payload.get('status', '')).lower()
    reason = str(payload.get('reason', '')).strip()[:1000]
    if status not in {'passed', 'failed', 'skipped', 'original'}:
        return jsonify({'error': 'Status must be passed, failed, skipped, or original'}), 400
    if status != 'original' and not reason:
        return jsonify({'error': 'Explain why this automated result should be overridden'}), 400

    result = dict(source.get('result') or {})
    tests = [dict(item) for item in result.get('tests_run') or [] if isinstance(item, dict)]
    if index < 0 or index >= len(tests):
        return jsonify({'error': 'Test result was not found'}), 404
    item = tests[index]
    automated_status = str(item.get('automated_result') or item.get('result') or 'skipped').lower()
    if automated_status not in {'passed', 'failed', 'skipped'}:
        automated_status = 'skipped'
    item['automated_result'] = automated_status
    if status == 'original':
        item['result'] = automated_status
        item.pop('operator_override', None)
        action = f"restored the automated **{automated_status.upper()}** result"
    else:
        item['result'] = status
        item['operator_override'] = {
            'status': status,
            'reason': reason,
            'by': session.get('github_login') or 'local operator',
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        }
        action = f"changed **{automated_status.upper()}** to **{status.upper()}**"

    statuses = [str(test.get('result', '')).lower() for test in tests]
    # A skipped proof is non-blocking in the Testing Lab. Only an explicit
    # failure prevents the ticket from reaching Done.
    overall = 'failed' if 'failed' in statuses else 'passed'
    result['tests_run'] = tests
    result['overall'] = overall
    result['override_allowed'] = True
    result.setdefault('automated_overall', source.get('result', {}).get('automated_overall') or source.get('result', {}).get('overall'))
    project_warning = ''
    if overall == 'passed':
        try:
            project_ops = GitHubOps(settings.command_timeout_seconds, lambda message: None)
            counts = project_ops.mark_issue_qa_done(parse_issue_url(source['issue_url']))
            status_count = counts.get('Status', 0)
            test_count = counts.get('Test State', 0)
            updated = bool(status_count and test_count)
            result['project_status'] = {
                'updated': updated, 'count': status_count, 'test_state_count': test_count,
                'status': 'Done', 'test_state': 'Pass',
            }
            if not updated:
                project_warning = 'Override saved, but the project is missing a Done Status or Pass Test State option.'
        except Exception as exc:
            project_warning = f'Override saved, but GitHub project fields could not update: {exc}'
            result['project_status'] = {
                'updated': False, 'count': 0, 'test_state_count': 0,
                'status': 'Done', 'test_state': 'Pass', 'warning': str(exc),
            }
    store.update(job_id, result_json=result)

    check_name = str(item.get('command') or f'Automated check {index + 1}').replace('\r', ' ').replace('\n', ' ').replace('`', "'")
    comment = (
        f"## QA operator override — {status.upper() if status != 'original' else 'RESET'}\n\n"
        f"Check: `{check_name[:500]}`\n\n"
        f"The operator {action}."
    )
    if reason:
        comment += f"\n\n**Reason:** {reason}"
    comment += f"\n\n**Recalculated QA outcome:** {overall.upper()}\n\n> The original machine result remains recorded in MergeQuest for auditability."
    try:
        post_issue_comment(source['issue_url'], comment)
    except Exception as exc:
        return jsonify({
            'ok': True,
            'warning': f'Override saved locally, but the GitHub correction could not be posted: {exc}',
            'result': result,
        })
    return jsonify({'ok': True, 'result': result, 'warning': project_warning})


@app.post("/testing/results")
def testing_results():
    payload = request.get_json(silent=True) or {}
    issue_url = str(payload.get('issue_url', '')).strip()
    results = payload.get('results')
    summary = str(payload.get('summary', '')).strip()
    if not issue_url or not isinstance(results, list) or not results:
        return jsonify({'error': 'Issue URL and at least one test result are required'}), 400
    try:
        issue = fetch_issue_from_github(issue_url)
        allowed = {'passed', 'failed', 'skipped'}
        normalized = []
        for item in results[:30]:
            step = str(item.get('step', '')).strip()[:500]
            status = str(item.get('status', '')).lower()
            notes = str(item.get('notes', '')).strip()[:1000]
            if not step or status not in allowed:
                return jsonify({'error': 'Every test needs a step and passed, failed, or skipped status'}), 400
            normalized.append((step, status, notes))
        counts = {status: sum(item[1] == status for item in normalized) for status in allowed}
        overall = 'FAILED' if counts['failed'] else ('PASSED' if counts['passed'] else 'INCOMPLETE')
        labels = {'passed': ('x', 'PASS'), 'failed': (' ', 'FAIL'), 'skipped': (' ', 'SKIPPED')}
        rows = []
        for step, status, notes in normalized:
            mark, label = labels[status]
            clean_step = step.replace('\r', ' ').replace('\n', ' ')
            clean_notes = notes.replace('\r', ' ').replace('\n', ' ')
            rows.append(f"- [{mark}] **{label}** — {clean_step}" + (f" — {clean_notes}" if clean_notes else ''))
        body = (
            f"## Automated test results — {overall}\n\nTicket: #{issue.get('number')} — {issue.get('title', '')}\n\n"
            + '\n'.join(rows)
            + f"\n\n**Totals:** {counts['passed']} passed · {counts['failed']} failed · {counts['skipped']} skipped"
        )
        if summary:
            body += f"\n\n**Tester summary:** {summary[:2000]}"
        post_issue_comment(issue_url, body + "\n\n> Captured by the MergeQuest autonomous integrity scanner.")
        return jsonify({'ok': True, 'overall': overall})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.get('/tickets')
def tickets():
    return jsonify({'tickets': store.list_tickets()})


@app.post('/tickets/import')
def import_tickets():
    try:
        path = Path(request.json.get('path', 'bug_tracker_items (1).xlsx')).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        imported = import_workbook(path)
        store.upsert_tickets(imported)
        return jsonify({'count': len(imported)})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.post('/tickets/test-plan')
def ticket_test_plan():
    key = request.json.get('key', '').strip()
    issue_url = request.json.get('issue_url', '').strip()
    if not key or not issue_url:
        return jsonify({'error': 'key and issue_url required'}), 400
    cached = store.get_ticket_test(key)
    if cached:
        return jsonify(cached)
    try:
        issue = fetch_issue_from_github(issue_url)
        safe_name = re.sub(r'[^A-Za-z0-9]+', '-', key)
        plan = generate_test_plan(
            settings.agent_command, settings.workspace_root, issue,
            settings.workspace_root / f'test-plan-{safe_name}.json',
            settings.review_timeout_seconds,
        )
        store.upsert_ticket_test(key, plan['repro_steps'], plan['pass_steps'])
        return jsonify(plan)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.post('/tickets/sync')
def sync_tickets():
    try:
        payload = request.get_json(silent=True) or {}
        repository = str(payload.get('repository') or '').strip()
        token = get_github_token()
        synced = sync_github(repository, token=token)
        store.prune_repository_tickets(repository, [ticket['key'] for ticket in synced])
        store.upsert_tickets(synced)
        eligible = store.list_tickets()
        if repository:
            eligible = [ticket for ticket in eligible if ticket.get('repository') == repository]
        return jsonify({'count': len(eligible), 'synced_count': len(synced)})
    except ValueError as exc:
        return jsonify({'error': str(exc), 'kind': 'validation'}), 422
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if 'not authenticated' in lowered or 'authentication token' in lowered or 'not logged' in lowered:
            status, kind = 401, 'authentication'
        elif 'permission' in lowered or 'forbidden' in lowered or 'http 403' in lowered:
            status, kind = 403, 'permission'
        elif 'could not access' in lowered or 'http 404' in lowered:
            status, kind = 404, 'repository'
        else:
            status, kind = 502, 'github'
        return jsonify({'error': message, 'kind': kind}), status


@app.post("/prompt/investigation")
def generate_investigation_prompt():
    issue_url = request.json.get("issue_url", "").strip()
    base_branch = request.json.get("base_branch", "develop").strip()
    branch_prefix = request.json.get("branch_prefix", "bug-fix").strip()

    if not issue_url:
        return jsonify({"error": "issue_url required"}), 400

    try:
        issue = fetch_issue_from_github(issue_url)
        branch_name = make_branch_name(branch_prefix, issue.get("number"), issue.get("title", ""))
        prompt = investigation_prompt(issue, base_branch, branch_name)
        return jsonify({"prompt": prompt})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/prompt/review")
def generate_review_prompt():
    issue_url = request.json.get("issue_url", "").strip()
    base_branch = request.json.get("base_branch", "develop").strip()

    if not issue_url:
        return jsonify({"error": "issue_url required"}), 400

    try:
        issue = fetch_issue_from_github(issue_url)
        prompt = review_prompt(issue, base_branch)
        return jsonify({"prompt": prompt})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/prompt/all-in-one")
def generate_all_in_one_prompt():
    issue_url = request.json.get("issue_url", "").strip()
    base_branch = request.json.get("base_branch", "develop").strip()
    branch_prefix = request.json.get("branch_prefix", "bug-fix").strip()
    if not issue_url:
        return jsonify({"error": "issue_url required"}), 400
    try:
        issue = fetch_issue_from_github(issue_url)
        branch_name = make_branch_name(branch_prefix, issue.get("number"), issue.get("title", ""))
        return jsonify({"prompt": all_in_one_prompt(issue, base_branch, branch_name)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _update_codex_in_background() -> None:
    """Best-effort: keep the Codex CLI current on every app start, without
    delaying startup or failing it if the update can't run (offline, etc)."""
    codex_bin = Path(settings.agent_command.split()[0]) if 'codex' in settings.agent_command else None
    if not codex_bin or not codex_bin.exists():
        return
    def run_update() -> None:
        try:
            subprocess.run(
                [str(codex_bin), "update"],
                env={**os.environ, "NPM_CONFIG_PREFIX": str(codex_bin.parent.parent)},
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            pass
    threading.Thread(target=run_update, daemon=True).start()


if __name__ == "__main__":
    _update_codex_in_background()
    port = find_available_port()
    print(f"Starting on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
