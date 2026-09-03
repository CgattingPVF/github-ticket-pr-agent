from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import shutil
import sys
import threading
import time
import webbrowser
import urllib.request
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request, redirect, url_for, session
from core import generate_test_plan, make_branch_name, parse_issue_url
from config import Settings
from prompts import all_in_one_prompt, investigation_prompt, review_prompt
from store import JobStore
from ticket_sync import ALLOWED_PROJECT_NUMBERS, find_gh_executable, import_workbook, sync_github
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


def fetch_historic_merged_pr_count(login: str) -> int:
    """Return the signed-in operator's merged-PR count for the initial baseline."""
    env = os.environ.copy()
    token = get_github_token()
    if token:
        env['GH_TOKEN'] = token
    result = subprocess.run(
        [
            find_gh_executable(), 'api', '-X', 'GET', 'search/issues',
            '-f', f'q=is:pr is:merged author:{login}', '-f', 'per_page=1',
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        creationflags=_subprocess_window_flags(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'Unable to calculate historic pull requests')
    try:
        return max(0, int(json.loads(result.stdout)['total_count']))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError('GitHub returned an invalid historic pull-request count') from exc


# Street-cred tiers set the operator title. Progression itself is event-based:
# historic merged PRs establish the baseline, then completed Contracts add to it.
STREET_CRED_TIERS = [
    {'rank': 'Back-Alley Runner', 'code': 'SC-01'},
    {'rank': 'Chrome Rookie', 'code': 'SC-02'},
    {'rank': 'Ghost Operator', 'code': 'SC-03'},
    {'rank': 'Netrunner', 'code': 'SC-04'},
    {'rank': 'Blackwall Specialist', 'code': 'SC-05'},
    {'rank': 'Afterlife Merc', 'code': 'SC-06'},
    {'rank': 'Night Legend', 'code': 'SC-07'},
    {'rank': 'City Icon', 'code': 'SC-08'},
]
XP_PER_LEVEL = 400
XP_COMPLETED = 120
EURODOLLARS_PER_HISTORIC_PR = 900
CONTRACT_REWARDS = {'P0': 2500, 'P1': 1800, 'P2': 1200}
DEFAULT_CONTRACT_REWARD = 900
CONTRACT_REPOSITORIES = (
    'pvfscaffolding/crm-staff-desktop',
    'pvfscaffolding/crm-api',
)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _completed_contracts(jobs: list[dict], baseline_at: str | None) -> list[dict]:
    """Return unique, reward-eligible Contracts completed after the PR baseline."""
    baseline = _parse_timestamp(baseline_at)
    contracts = {}
    for job in jobs:
        if job.get('status') != 'completed':
            continue
        if (job.get('parameters') or {}).get('workflow_profile') == 'testing_only':
            continue
        completed_at = _parse_timestamp(job.get('updated_at'))
        if baseline and (not completed_at or completed_at <= baseline):
            continue
        key = str(job.get('issue_url') or job.get('id') or '').strip().rstrip('/').lower()
        if key:
            contracts.setdefault(key, job)
    return list(contracts.values())


def _contract_reward(job: dict) -> int:
    try:
        return max(0, int((job.get('parameters') or {}).get(
            'contract_reward', DEFAULT_CONTRACT_REWARD,
        )))
    except (TypeError, ValueError):
        return DEFAULT_CONTRACT_REWARD


def player_stats(
    jobs: list[dict], *, historic_prs: int = 0, baseline_at: str | None = None,
) -> dict:
    """Derive static progression from historic PRs and completed Contracts."""
    historic_prs = max(0, int(historic_prs or 0))
    completed = _completed_contracts(jobs, baseline_at)
    failed = [j for j in jobs if j.get('status') == 'failed']
    xp = (historic_prs + len(completed)) * XP_COMPLETED
    level = 1 + xp // XP_PER_LEVEL
    tier = STREET_CRED_TIERS[min(level - 1, len(STREET_CRED_TIERS) - 1)]
    next_rank = STREET_CRED_TIERS[level] if level < len(STREET_CRED_TIERS) else None
    rank = tier['rank']

    banked = (
        historic_prs * EURODOLLARS_PER_HISTORIC_PR
        + sum(_contract_reward(job) for job in completed)
    )
    network_assets = historic_prs + len(completed)
    # A quarter of earned rewards is automatically reinvested in Blackwall
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
        'historic_prs': historic_prs,
        'network_assets': network_assets,
        'credits_rate': 0,
        'credits_banked': banked,
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

_codex_update_lock = threading.Lock()
_codex_update_state = {
    'status': 'idle',
    'started': False,
    'checked_at': None,
    'completed_at': None,
    'before_version': '',
    'after_version': '',
    'message': 'Codex CLI update has not started.',
    'error': '',
}

THEME_COOKIE = 'mergequest_theme'
DEFAULT_UI_THEME = 'cyberpunk'
UI_THEMES = {
    'cyberpunk': {
        'id': 'cyberpunk',
        'label': 'Cyberpunk 2077',
        'short_name': 'Neon Ops',
        'preview_label': 'NEON OPS / 2077',
        'stylesheet': None,
        'theme_color': '#07090d',
        'favicon': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%2307090d'/><path d='M12 58 42 10h24L48 43h38L51 92H27l24-34z' fill='%23fcee09'/></svg>",
        'description': 'Electric Night City yellows, cyan telemetry, sharp chrome rails, and animated network glow.',
        'brand_line': 'NEON OPS // BUILD 2.077',
        'badge': 'NETRUNNER OS',
        'status_title': 'MergeQuest system mood',
        'status': 'SECURITY GATES ONLINE',
        'moods': [
            'SECURITY GATES ONLINE',
            'COFFEE BUFFER: OPTIMAL',
            'BUGS FEAR THIS TERMINAL',
            'VIBES: COMPILED',
            'MERGE LUCK: +7',
        ],
        'sign_in': 'Jack in // GitHub',
        'sign_out': 'Jack out',
        'nav': ['Contracts', 'Prompt forge', 'Testing lab', 'Merc rankings', 'System'],
        'footer_name': 'MERGEQUEST // NEON OPS',
        'footer_copy': 'Evidence-gated autonomous operations · Human authorization owns every merge.',
        'logout_eyebrow': 'NEURAL LINK TERMINATION',
        'logout_title': 'JACKING OUT',
        'logout_start': 'Purging authentication shard…',
        'logout_step_one': 'Severing GitHub uplink…',
        'logout_step_two': 'Wiping operator trace…',
        'logout_complete': 'CONNECTION TERMINATED',
        'logout_goodbye': 'Safe travels, runner.',
    },
    'wwii': {
        'id': 'wwii',
        'label': 'World War II',
        'short_name': 'War Room 1944',
        'preview_label': 'FIELD HQ / 1944',
        'stylesheet': 'theme-wwii.css',
        'theme_color': '#26281f',
        'favicon': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='8' fill='%2326281f'/><path d='m50 12 9 27h29L65 56l9 28-24-17-24 17 9-28-23-17h29z' fill='%23d8c48a'/></svg>",
        'description': 'Olive drab command boards, weathered paper dossiers, brass details, and field-radio telemetry.',
        'brand_line': 'WAR ROOM // FIELD HQ 1944',
        'badge': 'ALLIED FIELD COMMAND',
        'status_title': 'MergeQuest field dispatch',
        'status': 'COMMAND POST OPERATIONAL',
        'moods': [
            'COMMAND POST OPERATIONAL',
            'RADIO WATCH: CLEAR',
            'COFFEE RATION: SECURED',
            'ORDERS AUTHENTICATED',
            'MORALE: STEADY',
        ],
        'sign_in': 'Report in // GitHub',
        'sign_out': 'Stand down',
        'nav': ['Orders', 'Codebook', 'Proving ground', 'Officer board', 'Settings'],
        'footer_name': 'MERGEQUEST // WAR ROOM 1944',
        'footer_copy': 'Evidence-gated field operations · Human command authorizes every merge.',
        'ranks': {
            'SC-01': 'Dispatch Recruit',
            'SC-02': 'Radio Operator',
            'SC-03': 'Field Technician',
            'SC-04': 'Recon Specialist',
            'SC-05': 'Signals Officer',
            'SC-06': 'Operations Captain',
            'SC-07': 'Command Major',
            'SC-08': 'HQ Strategist',
        },
        'achievements': {
            'First Blood': {'name': 'First Deployment', 'desc': 'Complete your first assignment'},
            'Triple Breach': {'name': 'Three-Order Ribbon', 'desc': 'Complete 3 assignments'},
            'Daemon Hunter': {'name': 'Veteran Operator', 'desc': 'Complete 10 assignments'},
            'Zero Trace': {'name': 'Clean Inspection', 'desc': 'Deploy with zero review findings'},
            'Overclocked': {'name': 'Sustained Advance', 'desc': 'Complete assignments 3 days in a row'},
            'Data Scavenger': {'name': 'Recovery Detail', 'desc': 'Recover evidence from 5 interrupted assignments'},
            'Second Heart': {'name': 'Second Wind', 'desc': 'Complete an assignment after a failed operation'},
        },
        'logout_eyebrow': 'FIELD RADIO SIGN-OFF',
        'logout_title': 'STANDING DOWN',
        'logout_start': 'Closing the secure channel…',
        'logout_step_one': 'Signing off with GitHub command…',
        'logout_step_two': 'Filing the final field report…',
        'logout_complete': 'CHANNEL CLOSED',
        'logout_goodbye': 'Until the next briefing.',
    },
    'hacker': {
        'id': 'hacker',
        'label': 'Hacker',
        'short_name': 'Root Shell',
        'preview_label': 'ROOT SHELL / TTY-01',
        'stylesheet': 'theme-hacker.css',
        'theme_color': '#020805',
        'favicon': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='5' fill='%23020805'/><path d='m18 28 22 22-22 22M48 72h34' fill='none' stroke='%234cff88' stroke-width='10' stroke-linecap='square'/></svg>",
        'description': 'Black-glass terminals, phosphor-green traces, amber alerts, scanlines, and uncompromising root-console telemetry.',
        'brand_line': 'ROOT SHELL // SECURE CONSOLE',
        'badge': 'ENCRYPTED SESSION',
        'status_title': 'MergeQuest root-shell status',
        'status': 'ROOT ACCESS: ACTIVE',
        'moods': [
            'ROOT ACCESS: ACTIVE',
            'FIREWALL: HARDENED',
            'PACKET TRACE: CLEAN',
            'ZERO-DAYS: CONTAINED',
            'COFFEE.EXE: RUNNING',
        ],
        'sign_in': 'Authenticate // GitHub',
        'sign_out': 'Terminate session',
        'nav': ['Targets', 'Payload lab', 'Sandbox', 'Operator ranks', 'Config'],
        'footer_name': 'MERGEQUEST // ROOT SHELL',
        'footer_copy': 'Evidence-gated red-team operations · Human authorization controls every merge.',
        'ranks': {
            'SC-01': 'Script Initiate',
            'SC-02': 'Packet Scout',
            'SC-03': 'Shell Operator',
            'SC-04': 'Exploit Analyst',
            'SC-05': 'Root Engineer',
            'SC-06': 'Red Team Lead',
            'SC-07': 'Zero-Day Hunter',
            'SC-08': 'Ghost in the Stack',
        },
        'achievements': {
            'First Blood': {'name': 'First Root', 'desc': 'Complete your first intrusion'},
            'Triple Breach': {'name': 'Triple Handshake', 'desc': 'Complete 3 intrusions'},
            'Daemon Hunter': {'name': 'Daemon Tamer', 'desc': 'Complete 10 intrusions'},
            'Zero Trace': {'name': 'Zero Trace', 'desc': 'Deploy with zero review findings'},
            'Overclocked': {'name': 'Persistent Session', 'desc': 'Complete intrusions 3 days in a row'},
            'Data Scavenger': {'name': 'Packet Recovery', 'desc': 'Recover evidence from 5 interrupted intrusions'},
            'Second Heart': {'name': 'Process Respawn', 'desc': 'Complete an intrusion after a failed operation'},
        },
        'logout_eyebrow': 'SECURE SESSION TERMINATION',
        'logout_title': 'LOGGING OFF',
        'logout_start': 'Revoking active shell token…',
        'logout_step_one': 'Closing GitHub tunnel…',
        'logout_step_two': 'Sanitizing operator history…',
        'logout_complete': 'SESSION TERMINATED',
        'logout_goodbye': 'Trace cleared. Stay curious.',
    },
    'skyrim': {
        'id': 'skyrim',
        'label': 'Skyrim',
        'short_name': 'Nordic Saga',
        'preview_label': 'NORDIC SAGA / IV',
        'stylesheet': 'theme-skyrim.css',
        'theme_color': '#1a2026',
        'favicon': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='8' fill='%231a2026'/><path d='M50 10 78 50 50 90 22 50Zm0 18L35 50l15 22 15-22Z' fill='%23b9d7de'/><circle cx='50' cy='50' r='7' fill='%23b88a4a'/></svg>",
        'description': 'Carved slate, weathered parchment, forged iron, warm bronze, and frost-lit runes from an original Nordic saga.',
        'brand_line': 'NORDIC SAGA // QUEST LEDGER',
        'badge': 'GUILD COMMAND',
        'status_title': 'MergeQuest hall watch',
        'status': 'THE WATCHTOWER STANDS',
        'moods': [
            'THE WATCHTOWER STANDS',
            'QUEST BOARD: UPDATED',
            'THE FORGE BURNS BRIGHT',
            'SKIES: FROST-CLEAR',
            'WARD SIGILS: STEADY',
        ],
        'sign_in': 'Enter the guild // GitHub',
        'sign_out': 'Leave the hall',
        'nav': ['Quests', 'Arcane codex', 'Trial grounds', 'Hall of heroes', 'Settings'],
        'footer_name': 'MERGEQUEST // NORDIC SAGA',
        'footer_copy': 'Evidence-bound guild quests · The guildmaster authorizes every merge.',
        'ranks': {
            'SC-01': 'Wanderer',
            'SC-02': 'Guild Initiate',
            'SC-03': 'Shield-Bearer',
            'SC-04': 'Rune Adept',
            'SC-05': 'Thane',
            'SC-06': 'Guildmaster',
            'SC-07': 'Frostborn Hero',
            'SC-08': 'Legend of the North',
        },
        'achievements': {
            'First Blood': {'name': 'First Quest', 'desc': 'Complete your first quest'},
            'Triple Breach': {'name': 'Three-Quest Sigil', 'desc': 'Complete 3 quests'},
            'Daemon Hunter': {'name': 'Veteran Adventurer', 'desc': 'Complete 10 quests'},
            'Zero Trace': {'name': 'Flawless Enchantment', 'desc': 'Deploy with zero review findings'},
            'Overclocked': {'name': 'Three-Day Campaign', 'desc': 'Complete quests 3 days in a row'},
            'Data Scavenger': {'name': 'Relic Recovery', 'desc': 'Recover evidence from 5 interrupted quests'},
            'Second Heart': {'name': 'Second Wind', 'desc': 'Complete a quest after a failed operation'},
        },
        'logout_eyebrow': 'RETURN TO THE HEARTH',
        'logout_title': 'LEAVING THE HALL',
        'logout_start': 'Closing the guild ledger…',
        'logout_step_one': 'Lowering the GitHub drawbridge…',
        'logout_step_two': 'Sealing the final quest record…',
        'logout_complete': 'THE GATE IS SEALED',
        'logout_goodbye': 'May the road lead you home.',
    },
    'professional': {
        'id': 'professional', 'label': 'Professional Work', 'short_name': 'Workstation',
        'preview_label': 'WORKSTATION / HQ', 'stylesheet': 'theme-professional.css',
        'theme_color': '#f4f7fb',
        'favicon': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='12' fill='%23f4f7fb'/><rect x='16' y='16' width='68' height='68' rx='10' fill='%231769e0'/><path d='m30 50 13 13 27-29' fill='none' stroke='white' stroke-width='9' stroke-linecap='round' stroke-linejoin='round'/></svg>",
        'description': 'A calm enterprise workspace with slate surfaces, cobalt accents, clear status states, and focused density.',
        'brand_line': 'WORKSTATION // OPERATIONS HUB', 'badge': 'CONTROLLED WORKSPACE',
        'status_title': 'MergeQuest workspace status', 'status': 'WORKSPACE OPERATIONAL',
        'moods': ['WORKSPACE OPERATIONAL', 'SYNC HEALTH: GOOD', 'REVIEW QUEUE: CLEAR', 'COFFEE BREAK: OPTIONAL', 'MERGE WINDOW: OPEN'],
        'sign_in': 'Connect // GitHub', 'sign_out': 'Sign out',
        'nav': ['Work queue', 'Prompt studio', 'QA workspace', 'Team insights', 'Settings'],
        'footer_name': 'MERGEQUEST // WORKSTATION',
        'footer_copy': 'Evidence-led delivery operations · Human approval remains in control.',
        'ranks': {'SC-01': 'Associate', 'SC-02': 'Contributor', 'SC-03': 'Specialist', 'SC-04': 'Senior Specialist', 'SC-05': 'Lead Engineer', 'SC-06': 'Delivery Manager', 'SC-07': 'Principal Engineer', 'SC-08': 'Practice Lead'},
        'achievements': {'First Blood': {'name': 'First Delivery', 'desc': 'Complete your first delivery'}, 'Triple Breach': {'name': 'Three Deliveries', 'desc': 'Complete 3 deliveries'}, 'Daemon Hunter': {'name': 'Delivery Veteran', 'desc': 'Complete 10 deliveries'}, 'Zero Trace': {'name': 'Clean Review', 'desc': 'Deploy with zero review findings'}, 'Overclocked': {'name': 'Steady Momentum', 'desc': 'Complete deliveries 3 days in a row'}, 'Data Scavenger': {'name': 'Recovery Specialist', 'desc': 'Recover evidence from 5 interrupted deliveries'}, 'Second Heart': {'name': 'Resilient Delivery', 'desc': 'Complete a delivery after a failed operation'}},
        'logout_eyebrow': 'WORKSPACE SIGN-OUT', 'logout_title': 'SIGNING OFF',
        'logout_start': 'Closing your workspace session…', 'logout_step_one': 'Disconnecting GitHub workspace…', 'logout_step_two': 'Saving operator activity…',
        'logout_complete': 'SESSION CLOSED', 'logout_goodbye': 'See you in the workspace.',
    },
    'pvf': {
        'id': 'pvf', 'label': 'PVF Scaffolding', 'short_name': 'PVF Yard',
        'preview_label': 'PVF YARD / SITE', 'stylesheet': 'theme-pvf.css',
        'theme_color': '#eef2ec',
        'favicon': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='12' fill='%23eef2ec'/><g stroke='%235a8f2c' stroke-width='7' stroke-linecap='round'><path d='M28 18v64M72 18v64M28 34h44M28 58h44M28 82h44'/></g></svg>",
        'description': 'PVF Scaffolding brand green on clean slate — a bright site-office workspace with a scaffold-build sync animation.',
        'brand_line': 'PVF YARD // SITE OPERATIONS', 'badge': 'PVF WORKSPACE',
        'status_title': 'PVF yard status', 'status': 'YARD OPERATIONAL',
        'moods': ['YARD OPERATIONAL', 'SCAFFOLD INSPECTED', 'CREW ON SITE', 'TEA BREAK: BOOKED', 'HANDOVER: ON SCHEDULE'],
        'sign_in': 'Sign in // GitHub', 'sign_out': 'Sign out',
        'nav': ['Job board', 'Prompt studio', 'Site checks', 'Crew board', 'Settings'],
        'footer_name': 'MERGEQUEST // PVF YARD',
        'footer_copy': 'Evidence-led site operations · Human approval signs off every merge.',
        'ranks': {'SC-01': 'Labourer', 'SC-02': 'Scaffolder', 'SC-03': 'Advanced Scaffolder', 'SC-04': 'Charge Hand', 'SC-05': 'Foreman', 'SC-06': 'Site Supervisor', 'SC-07': 'Contracts Manager', 'SC-08': 'Yard Director'},
        'achievements': {'First Blood': {'name': 'First Lift', 'desc': 'Complete your first job'}, 'Triple Breach': {'name': 'Three Lifts', 'desc': 'Complete 3 jobs'}, 'Daemon Hunter': {'name': 'Yard Veteran', 'desc': 'Complete 10 jobs'}, 'Zero Trace': {'name': 'Clean Handover', 'desc': 'Deploy with zero review findings'}, 'Overclocked': {'name': 'Steady Crew', 'desc': 'Complete jobs 3 days in a row'}, 'Data Scavenger': {'name': 'Salvage Crew', 'desc': 'Recover evidence from 5 interrupted jobs'}, 'Second Heart': {'name': 'Back On Site', 'desc': 'Complete a job after a failed operation'}},
        'logout_eyebrow': 'YARD SIGN-OUT', 'logout_title': 'SIGNING OFF',
        'logout_start': 'Closing your site session…', 'logout_step_one': 'Disconnecting GitHub workspace…', 'logout_step_two': 'Filing crew activity…',
        'logout_complete': 'SESSION CLOSED', 'logout_goodbye': 'Safe home — see you on site.',
    },
}

THEME_SETTINGS_COPY = {
    'cyberpunk': {
        'title': 'System configuration', 'eyebrow': 'RIPPERDOC // CONTROL ROOM',
        'intro': 'Choose the interface appearance and tune runner cyberware, neural thresholds, and local network parameters. Credentials remain air-gapped from this display.',
        'apply': 'Apply interface theme', 'agents': 'Runner matrix',
        'primary': 'Primary runner', 'reviewer': 'Counter-intel runner', 'claude': 'Claude daemon',
        'confidence': 'Neural confidence gate', 'recovery': 'Recovery cycles',
        'defaults_before': 'Cyberware defaults are loaded from', 'defaults_after': 'Individual operations can install temporary loadouts.',
        'telemetry': 'Runtime telemetry', 'root': 'Network root', 'storage': 'Shard storage',
        'command_timeout': 'Quickhack timeout', 'review_timeout': 'Black ICE timeout',
        'local': 'Local mirror', 'local_fallback': 'Mirrored per operation',
        'auth_before': 'neural authentication routes through the CLI. Run', 'auth_after': 'to scan the handshake.',
    },
    'wwii': {
        'title': 'Command settings', 'eyebrow': 'QUARTERMASTER // COMMAND TENT',
        'intro': 'Choose the field-room appearance and inspect agent, evidence, and local workspace orders. Credentials remain sealed from this report.',
        'apply': 'Issue appearance order', 'agents': 'Agent roster',
        'primary': 'Primary field engineer', 'reviewer': 'Independent reviewer', 'claude': 'Claude unit',
        'confidence': 'Evidence confidence gate', 'recovery': 'Recovery attempts',
        'defaults_before': 'Field defaults are issued from', 'defaults_after': 'Individual operations may carry temporary orders.',
        'telemetry': 'Field telemetry', 'root': 'Operations root', 'storage': 'Records storage',
        'command_timeout': 'Operation timeout', 'review_timeout': 'Inspection timeout',
        'local': 'Local depot', 'local_fallback': 'Prepared per operation',
        'auth_before': 'authentication is relayed through the command-line post. Run', 'auth_after': 'to inspect the channel.',
    },
    'hacker': {
        'title': 'Root configuration', 'eyebrow': 'ROOT TERMINAL // CONFIG',
        'intro': 'Select a console skin and inspect payload runners, trust thresholds, and local sandbox paths. Secret tokens stay redacted from this terminal.',
        'apply': 'Commit theme config', 'agents': 'Process table',
        'primary': 'Primary exploit runner', 'reviewer': 'Independent red-team reviewer', 'claude': 'Claude process',
        'confidence': 'Trust threshold', 'recovery': 'Respawn attempts',
        'defaults_before': 'Shell defaults are sourced from', 'defaults_after': 'Each intrusion may mount a temporary profile.',
        'telemetry': 'Kernel telemetry', 'root': 'Sandbox root', 'storage': 'Encrypted storage',
        'command_timeout': 'Payload timeout', 'review_timeout': 'Audit timeout',
        'local': 'Local clone', 'local_fallback': 'Cloned per intrusion',
        'auth_before': 'authentication is tunneled through the CLI. Run', 'auth_after': 'to verify the key exchange.',
    },
    'skyrim': {
        'title': 'Guild settings', 'eyebrow': "STEWARD'S TABLE // GREAT HALL",
        'intro': 'Choose the hall’s appearance and inspect guild agents, proof wards, and local workshop paths. Secret seals remain hidden from this ledger.',
        'apply': 'Seal appearance decree', 'agents': 'Guild retinue',
        'primary': 'Master artificer', 'reviewer': 'Independent lorekeeper', 'claude': 'Claude familiar',
        'confidence': 'Proof ward strength', 'recovery': 'Restoration attempts',
        'defaults_before': 'Guild customs are read from', 'defaults_after': 'Each quest may carry a temporary enchantment.',
        'telemetry': 'Scrying ledger', 'root': 'Workshop root', 'storage': 'Archive vault',
        'command_timeout': 'Quest timeout', 'review_timeout': 'Lore review timeout',
        'local': 'Local workshop', 'local_fallback': 'Prepared per quest',
        'auth_before': 'authentication travels through the command-line courier. Run', 'auth_after': 'to inspect the guild seal.',
    },
}

THEME_SETTINGS_COPY['professional'] = {
    'title': 'Workspace settings', 'eyebrow': 'WORKSPACE ADMIN // CONFIGURATION',
    'intro': 'Choose the workspace appearance and review runner, evidence, and local project settings. Credentials remain protected from this view.',
    'apply': 'Save workspace theme', 'agents': 'Automation roster', 'primary': 'Primary implementation agent',
    'reviewer': 'Independent reviewer', 'claude': 'Claude assistant', 'confidence': 'Evidence confidence gate', 'recovery': 'Recovery attempts',
    'defaults_before': 'Workspace defaults are loaded from', 'defaults_after': 'Individual operations may use temporary settings.',
    'telemetry': 'Workspace telemetry', 'root': 'Project workspace', 'storage': 'Records storage',
    'command_timeout': 'Operation timeout', 'review_timeout': 'Review timeout', 'local': 'Local checkout', 'local_fallback': 'Prepared per operation',
    'auth_before': 'GitHub authentication uses the command line. Run', 'auth_after': 'to inspect the connection.',
}

PROFESSIONAL_DIALECT = {
    'home_title': 'Work queue', 'rank_label': 'DELIVERY SCORE', 'rank_unit': 'DS', 'mission': 'delivery', 'missions': 'deliveries', 'currency': 'CR', 'currency_name': 'Delivery credits',
    'secured': 'Deliveries completed', 'live': 'Operations live', 'fund_sigil': '✓', 'fund_eyebrow': 'AUTOMATIC ALLOCATION // DELIVERY ENABLEMENT', 'fund_title': 'Your credits keep the team moving.',
    'fund_body': '25% of earned PR baseline and delivery rewards is routed to shared tooling, evidence storage, and recovery capacity. Balances change only when a delivery is completed.', 'reserve': 'ENABLEMENT RESERVE', 'allocated': 'ALLOCATED',
    'incoming': 'incoming work items', 'search_placeholder': 'Search work item #1091', 'sync': 'Sync work queue', 'threats': {'P0': 'BLOCKER', 'P1': 'HIGH PRIORITY', 'P2': 'STANDARD', 'other': 'LOW RISK'}, 'open_item': 'Open work item', 'hide_item': 'Dismiss', 'briefing': 'DELIVERY BRIEF', 'empty': 'NO WORK ITEMS AVAILABLE', 'loadout': 'Delivery plan', 'loadout_ready': 'WORKSPACE READY', 'targets': 'Selected work items', 'full_strategy': 'FULL DELIVERY', 'recon_strategy': 'DISCOVERY ONLY', 'protocol': 'Delivery plan', 'protocol_hint': 'choose the workflow path', 'failure_report': 'Delivery exception report', 'advanced': 'Open advanced workspace settings', 'agent': 'agent', 'full_action': 'Start full delivery', 'fast_action': 'Start rapid delivery', 'archive_completed': 'DELIVERY COMPLETE', 'archive_failed': 'REPORT FILED', 'archive_active': 'IN PROGRESS', 'archive_closed': 'CLOSED', 'archive': 'Delivery archive', 'accolades': 'Professional milestones', 'accolades_state': 'earned',
    'prompt_title': 'Prompt studio', 'prompt_eyebrow': 'WORKSPACE STUDIO // BRIEFING DESK', 'prompt_heading': 'Prompt studio', 'prompt_intro': 'Sync the work queue, select an item, and prepare an investigation, review, or full delivery brief.', 'prompt_feed': 'Incoming work queue', 'prompt_regions': 'Project workspaces', 'prompt_sync': 'Sync GitHub workspace', 'prompt_search': 'Work item search', 'prompt_forge': 'Prepare delivery brief', 'prompt_output': 'Delivery brief // ready', 'prompt_copy': 'Copy brief',
    'testing_title': 'QA workspace', 'testing_eyebrow': 'QUALITY OPERATIONS // VERIFICATION DESK', 'testing_heading': 'Evidence review', 'testing_intro': 'Select a work item and inspect the latest automated test evidence. Clear, repeatable proof for every change.', 'testing_target': 'Work item desk', 'testing_agent': 'QA assistant', 'testing_run': 'Run evidence review', 'testing_waiting': 'Awaiting work item', 'testing_control': 'QA control center', 'testing_grade': 'EVIDENCE GRADE', 'testing_wire': 'LIVE QA FEED',
    'ranking_title': 'Team insights', 'ranking_eyebrow': 'DELIVERY OPERATIONS // TEAM DIRECTORY', 'ranking_heading': 'Delivery score // team index', 'ranking_live': 'LIVE GITHUB INSIGHTS', 'registered': 'REGISTERED TEAM MEMBER', 'unknown': 'Unknown teammate', 'assets': 'DELIVERY CREDITS', 'region': 'SELECT WORKSPACE', 'registry': 'Team directory', 'record_noun': 'deliveries', 'login_eyebrow': 'WORKSPACE CONNECT // GITHUB', 'login_title': 'Connect your GitHub workspace', 'job_full': 'Full Delivery', 'job_recon': 'Discovery Run', 'job_back': 'WORK QUEUE', 'job_insertion': 'WORKSPACE', 'job_assigned': 'ASSIGNED', 'job_gates': 'Delivery gates', 'overlay_command': 'WORKSPACE OPERATIONS', 'overlay_title': 'Work queue sync in progress', 'overlay_window': 'SYNC WINDOW', 'overlay_status': 'CONNECTING TO WORKSPACE', 'overlay_timeline': 'Sync timeline', 'overlay_first': 'Open workspace connection',
}

PROFESSIONAL_SYNC_COPY = {
    'aria': 'Workspace sync status', 'terminal': 'workspace@mergequest:~', 'code': 'WORK-01', 'prompt': 'operator@workspace:~$', 'transfer': 'ITEMS', 'sequence_label': 'WORKSPACE SYNC',
    'stages': [{'title': 'Open workspace connection', 'detail': 'Establishing GitHub workspace channel'}, {'title': 'Load work items', 'detail': 'Fetching issue and project work items'}, {'title': 'Check priorities', 'detail': 'Scoring priorities and assignments'}, {'title': 'Update work queue', 'detail': 'Preparing your refreshed work queue'}],
    'initial_log': 'Workspace connection requested…', 'live': 'LIVE WORKSPACE SYNC', 'caution': 'KEEP THIS WINDOW OPEN',
    'line_prefix': '',
    'terminal_lines': ['Hi! Starting your workspace sync now.', 'Connecting to GitHub… you can keep this window open.', 'I’ll pull issues, projects, and priorities for you.', 'This usually takes a few seconds.'],
    'sequence': [{'status': 'CONNECTING TO WORKSPACE', 'log': 'Workspace connection requested…'}, {'status': 'LOADING WORK ITEMS', 'log': 'Receiving project work items…'}, {'status': 'CHECKING PRIORITIES', 'log': 'Normalizing priorities, owners, and project states…'}, {'status': 'UPDATING WORK QUEUE', 'log': 'Compiling the refreshed work queue…'}],
    'confirm_status': 'VERIFYING WORKSPACE RESPONSE', 'confirm_log': 'All work items received. Verifying workspace response…', 'success_status': 'SYNC COMPLETE // WORK QUEUE UPDATED', 'failure_status': 'SYNC FAILED // WORKSPACE CONNECTION REJECTED', 'success_log': 'Workspace verified. Work queue ready.', 'failure_log': 'Workspace rejected the sync. Connection safely closed.', 'button_active': 'Workspace sync active…', 'button_idle': 'Sync work queue', 'error_prefix': 'Workspace sync failed: ',
}

THEME_DIALECT = {
    'cyberpunk': {
        'home_title': 'Contracts', 'rank_label': 'STREET CRED', 'rank_unit': 'SC',
        'mission': 'Contract', 'missions': 'contracts', 'currency': '€$', 'currency_name': 'Eurodollars €$',
        'secured': 'Targets flatlined', 'live': 'Breaches live', 'fund_sigil': '₿',
        'fund_eyebrow': 'AUTOMATIC REINVESTMENT // BLACKWALL MAINTENANCE',
        'fund_title': 'Your EuroDollars keep the network alive.',
        'fund_body': '25% of earned PR baseline and Contract rewards is routed to perimeter shielding, cold storage, and emergency extraction. Balances change only when a Contract is completed.',
        'reserve': 'BLACKWALL RESERVE', 'allocated': 'REINVESTED',
        'incoming': 'incoming contracts', 'search_placeholder': 'Search target #1091',
        'sync': 'Breach // Resync', 'threats': {'P0': 'CYBERPSYCHO', 'P1': 'HOSTILE', 'P2': 'WATCHED', 'other': 'CLEAN'},
        'open_item': 'Open shard', 'hide_item': 'Purge', 'briefing': 'RECON PROTOCOL',
        'empty': 'NO CONTRACT SIGNALS', 'loadout': 'Runner loadout', 'loadout_ready': 'SYSTEM READY',
        'targets': 'Contract targets', 'full_strategy': 'AUTONOMOUS DAEMON', 'recon_strategy': 'GHOST PROTOCOL',
        'protocol': 'Operation protocol', 'protocol_hint': 'choose your risk curve', 'failure_report': 'Dead-drop failure intel',
        'advanced': 'Open ripperdoc loadout', 'agent': 'runner', 'full_action': 'Initiate deep breach', 'fast_action': 'Initiate quickhack',
        'archive_completed': 'TARGET FLATLINED', 'archive_failed': 'INTEL EXTRACTED', 'archive_active': 'BREACH ACTIVE', 'archive_closed': 'SIGNAL LOST',
        'archive': 'Operation archive', 'accolades': 'Encrypted accolades', 'accolades_state': 'decrypted',
        'prompt_title': 'Prompt forge', 'prompt_eyebrow': 'DATA SHARD FABRICATION',
        'prompt_heading': 'Prompt forge', 'prompt_intro': 'Sync the contract network, lock a target, and forge an investigation, audit, or full-operation protocol.',
        'prompt_feed': 'Incoming contract feed', 'prompt_regions': 'Network districts', 'prompt_sync': 'Scan GitHub network',
        'prompt_search': 'Target search', 'prompt_forge': 'Forge data shard', 'prompt_output': 'Data shard // ready', 'prompt_copy': 'Copy shard',
        'testing_title': 'Testing lab', 'testing_eyebrow': 'BLACKWALL // AUTONOMOUS QA GRID',
        'testing_heading': 'Integrity scanner', 'testing_intro': 'Lock onto a ticket and pull the latest automated test telemetry from its workflow run. No manual checkbox ritual—just machine evidence.',
        'testing_target': 'Target uplink', 'testing_agent': 'QA daemon', 'testing_run': 'Initiate integrity scan',
        'testing_waiting': 'Awaiting target', 'testing_control': 'QA Mission Control', 'testing_grade': 'SCAN RANK',
        'testing_wire': 'LIVE DEEP SCAN',
        'ranking_title': 'Merc rankings', 'ranking_eyebrow': 'AFTERLIFE MERC REGISTRY',
        'ranking_heading': 'Street cred // operator index', 'ranking_live': 'LIVE GITHUB INTELLIGENCE',
        'registered': 'REGISTERED OPERATOR', 'unknown': 'Unknown Merc', 'assets': 'LIQUID ASSETS',
        'region': 'SCAN DISTRICT', 'registry': 'Merc registry', 'record_noun': 'payloads',
        'login_eyebrow': 'NEURAL HANDSHAKE // GITHUB', 'login_title': 'Jack into GitHub',
        'job_full': 'Autonomous Daemon', 'job_recon': 'Ghost Protocol', 'job_back': 'CONTRACTS',
        'job_insertion': 'INSERTION', 'job_assigned': 'JACKED IN', 'job_gates': 'Safety gates',
        'overlay_command': 'NETWORK OPERATIONS', 'overlay_title': 'Breach in progress',
        'overlay_window': 'UPLINK WINDOW', 'overlay_status': 'INITIALIZING NEURAL HANDSHAKE',
        'overlay_timeline': 'Operation timeline', 'overlay_first': 'Open uplink',
    },
    'wwii': {
        'home_title': 'Orders', 'rank_label': 'MERIT POINTS', 'rank_unit': 'MP',
        'mission': 'assignment', 'missions': 'assignments', 'currency': 'WB', 'currency_name': 'War bonds',
        'secured': 'Orders secured', 'live': 'Operations live', 'fund_sigil': '★',
        'fund_eyebrow': 'QUARTERMASTER ALLOCATION // SIGNAL CORPS RESERVE',
        'fund_title': 'Your war bonds keep the field network moving.',
        'fund_body': '25% of earned PR baseline and assignment rewards is routed to field radios, the motor pool, and emergency extraction. Balances change only when an assignment is completed.',
        'reserve': 'SIGNAL CORPS RESERVE', 'allocated': 'ALLOCATED',
        'incoming': 'incoming orders', 'search_placeholder': 'Search orders #1091',
        'sync': 'Radio HQ // Refresh', 'threats': {'P0': 'CRITICAL', 'P1': 'PRIORITY', 'P2': 'ROUTINE', 'other': 'CLEAR'},
        'open_item': 'Open dossier', 'hide_item': 'File', 'briefing': 'FIELD BRIEFING',
        'empty': 'NO FIELD ORDERS RECEIVED', 'loadout': 'Field kit', 'loadout_ready': 'ORDERS DESK READY',
        'targets': 'Assignment orders', 'full_strategy': 'FULL CAMPAIGN', 'recon_strategy': 'RECON PATROL',
        'protocol': 'Mission plan', 'protocol_hint': 'choose the deployment order', 'failure_report': 'Field failure report',
        'advanced': 'Open advanced field kit', 'agent': 'unit', 'full_action': 'Begin full offensive', 'fast_action': 'Begin rapid sortie',
        'archive_completed': 'ORDER SECURED', 'archive_failed': 'FIELD REPORT FILED', 'archive_active': 'OPERATION ACTIVE', 'archive_closed': 'ORDER CLOSED',
        'archive': 'Field archive', 'accolades': 'Service ribbons', 'accolades_state': 'awarded',
        'prompt_title': 'Codebook', 'prompt_eyebrow': 'FIELD CODEBOOK // BRIEFING ROOM',
        'prompt_heading': 'Operations codebook', 'prompt_intro': 'Receive field orders, select an assignment, and prepare an investigation, inspection, or full-operation briefing.',
        'prompt_feed': 'Incoming field orders', 'prompt_regions': 'Operational theatres', 'prompt_sync': 'Radio GitHub command',
        'prompt_search': 'Order search', 'prompt_forge': 'Prepare field briefing', 'prompt_output': 'Field briefing // ready', 'prompt_copy': 'Copy briefing',
        'testing_title': 'Proving ground', 'testing_eyebrow': 'SIGNALS CORPS // INSPECTION DESK',
        'testing_heading': 'Field inspection', 'testing_intro': 'Assign an order and receive the latest automated inspection record from its workflow run. No paper checklist ritual—just machine evidence.',
        'testing_target': 'Order desk', 'testing_agent': 'Inspection unit', 'testing_run': 'Begin field inspection',
        'testing_waiting': 'Awaiting orders', 'testing_control': 'Inspection command', 'testing_grade': 'INSPECTION GRADE',
        'testing_wire': 'LIVE INSPECTION WIRE',
        'ranking_title': 'Officer board', 'ranking_eyebrow': 'FIELD COMMAND // SERVICE ROSTER',
        'ranking_heading': 'Service record // officer index', 'ranking_live': 'LIVE GITHUB DISPATCHES',
        'registered': 'REGISTERED FIELD OFFICER', 'unknown': 'Unknown Officer', 'assets': 'WAR BONDS',
        'region': 'SELECT THEATRE', 'registry': 'Officer roster', 'record_noun': 'dispatches',
        'login_eyebrow': 'SECURE RADIO // GITHUB COMMAND', 'login_title': 'Report to GitHub command',
        'job_full': 'Field Operation', 'job_recon': 'Recon Patrol', 'job_back': 'ORDERS',
        'job_insertion': 'DEPLOYMENT', 'job_assigned': 'ASSIGNED', 'job_gates': 'Inspection gates',
        'overlay_command': 'FIELD COMMAND', 'overlay_title': 'Radio dispatch in progress',
        'overlay_window': 'RADIO WINDOW', 'overlay_status': 'TUNING HQ FREQUENCY',
        'overlay_timeline': 'Dispatch timeline', 'overlay_first': 'Tune field radio',
    },
    'hacker': {
        'home_title': 'Targets', 'rank_label': 'ROOT REP', 'rank_unit': 'RP',
        'mission': 'intrusion', 'missions': 'intrusions', 'currency': 'CR', 'currency_name': 'Encrypted credits',
        'secured': 'Exploits shipped', 'live': 'Sessions live', 'fund_sigil': 'λ',
        'fund_eyebrow': 'AUTOMATIC ALLOCATION // FIREWALL HARDENING',
        'fund_title': 'Your credits keep the secure stack online.',
        'fund_body': '25% of earned PR baseline and intrusion rewards is routed to hardened relays, encrypted storage, and emergency rollback. Balances change only when an intrusion completes.',
        'reserve': 'COLD WALLET RESERVE', 'allocated': 'ALLOCATED',
        'incoming': 'incoming targets', 'search_placeholder': 'Search target #1091',
        'sync': 'Probe // Rescan', 'threats': {'P0': 'ROOT CRITICAL', 'P1': 'EXPOSED', 'P2': 'MONITORED', 'other': 'CLEAN'},
        'open_item': 'Inspect packet', 'hide_item': 'Drop', 'briefing': 'EXPLOIT PLAN',
        'empty': 'NO TARGETS ON WIRE', 'loadout': 'Payload stack', 'loadout_ready': 'SHELL READY',
        'targets': 'Intrusion targets', 'full_strategy': 'ROOT DAEMON', 'recon_strategy': 'GHOST SHELL',
        'protocol': 'Execution protocol', 'protocol_hint': 'select an intrusion profile', 'failure_report': 'Crash dump report',
        'advanced': 'Open advanced shell config', 'agent': 'process', 'full_action': 'Execute root operation', 'fast_action': 'Run rapid exploit',
        'archive_completed': 'EXPLOIT SHIPPED', 'archive_failed': 'CRASH DUMP SAVED', 'archive_active': 'SESSION ACTIVE', 'archive_closed': 'SESSION CLOSED',
        'archive': 'Session archive', 'accolades': 'Access badges', 'accolades_state': 'unlocked',
        'prompt_title': 'Payload lab', 'prompt_eyebrow': 'PAYLOAD COMPILER // SECURE BUFFER',
        'prompt_heading': 'Payload laboratory', 'prompt_intro': 'Scan the target feed, lock an endpoint, and compile an investigation, audit, or full-intrusion protocol.',
        'prompt_feed': 'Incoming target stream', 'prompt_regions': 'Network segments', 'prompt_sync': 'Probe GitHub endpoints',
        'prompt_search': 'Packet search', 'prompt_forge': 'Compile payload brief', 'prompt_output': 'Payload // compiled', 'prompt_copy': 'Copy payload',
        'testing_title': 'Sandbox', 'testing_eyebrow': 'AIR GAP // VERIFICATION SANDBOX',
        'testing_heading': 'Exploit verifier', 'testing_intro': 'Select a target and stream the latest automated test evidence from its isolated workflow. Raw machine proof, no checkbox theatre.',
        'testing_target': 'Sandbox target', 'testing_agent': 'Audit process', 'testing_run': 'Execute verification',
        'testing_waiting': 'Awaiting packet', 'testing_control': 'Sandbox Control', 'testing_grade': 'TRUST LEVEL',
        'testing_wire': 'LIVE PROCESS TRACE',
        'ranking_title': 'Operator ranks', 'ranking_eyebrow': 'ROOT DIRECTORY // OPERATOR INDEX',
        'ranking_heading': 'Root reputation // operator index', 'ranking_live': 'LIVE GITHUB PACKETS',
        'registered': 'AUTHENTICATED OPERATOR', 'unknown': 'Unknown User', 'assets': 'ENCRYPTED CREDITS',
        'region': 'SELECT SEGMENT', 'registry': 'Operator registry', 'record_noun': 'payloads',
        'login_eyebrow': 'KEY EXCHANGE // GITHUB', 'login_title': 'Authenticate with GitHub',
        'job_full': 'Root Daemon', 'job_recon': 'Ghost Shell', 'job_back': 'TARGETS',
        'job_insertion': 'ENTRY VECTOR', 'job_assigned': 'SESSION OPEN', 'job_gates': 'Audit gates',
        'overlay_command': 'ROOT OPERATIONS', 'overlay_title': 'Packet rescan in progress',
        'overlay_window': 'TRACE WINDOW', 'overlay_status': 'NEGOTIATING SECURE HANDSHAKE',
        'overlay_timeline': 'Process timeline', 'overlay_first': 'Open encrypted tunnel',
    },
    'skyrim': {
        'home_title': 'Quests', 'rank_label': 'RENOWN', 'rank_unit': 'RN',
        'mission': 'quest', 'missions': 'quests', 'currency': 'G', 'currency_name': 'Guild gold',
        'secured': 'Quests fulfilled', 'live': 'Adventures underway', 'fund_sigil': '✦',
        'fund_eyebrow': 'GUILD TITHE // WATCHTOWER RESTORATION',
        'fund_title': 'Your gold keeps the guild hall standing.',
        'fund_body': '25% of earned PR baseline and quest rewards is set aside for the watchtower, archive vault, and rescue provisions. The treasury changes only when a quest is fulfilled.',
        'reserve': 'WATCHTOWER RESERVE', 'allocated': 'TITHED',
        'incoming': 'new quests', 'search_placeholder': 'Search quest #1091',
        'sync': 'Consult courier // Refresh', 'threats': {'P0': 'DIRE', 'P1': 'PERILOUS', 'P2': 'WATCHED', 'other': 'CALM'},
        'open_item': 'Open scroll', 'hide_item': 'Shelve', 'briefing': 'QUEST NOTES',
        'empty': 'NO QUESTS PINNED', 'loadout': "Adventurer's pack", 'loadout_ready': 'GUILD READY',
        'targets': 'Chosen quests', 'full_strategy': 'GRAND EXPEDITION', 'recon_strategy': 'SCOUTING JOURNEY',
        'protocol': 'Quest path', 'protocol_hint': 'choose the journey ahead', 'failure_report': 'Wayfarer’s report',
        'advanced': 'Open the artificer’s satchel', 'agent': 'companion', 'full_action': 'Begin grand expedition', 'fast_action': 'Begin swift errand',
        'archive_completed': 'QUEST FULFILLED', 'archive_failed': 'TALE RECOVERED', 'archive_active': 'ADVENTURE UNDERWAY', 'archive_closed': 'CHRONICLE SEALED',
        'archive': 'Quest chronicle', 'accolades': 'Runic honors', 'accolades_state': 'bestowed',
        'prompt_title': 'Arcane codex', 'prompt_eyebrow': 'ARCANE CODEX // SCRIBES’ CHAMBER',
        'prompt_heading': 'Arcane codex', 'prompt_intro': 'Receive the guild’s quests, choose a scroll, and inscribe an investigation, lore review, or full expedition brief.',
        'prompt_feed': 'Guild quest board', 'prompt_regions': 'Northern holds', 'prompt_sync': 'Summon GitHub courier',
        'prompt_search': 'Quest search', 'prompt_forge': 'Inscribe quest brief', 'prompt_output': 'Quest scroll // ready', 'prompt_copy': 'Copy scroll',
        'testing_title': 'Trial grounds', 'testing_eyebrow': 'RUNEWARD // TRIALS CHAMBER',
        'testing_heading': 'Trial of proof', 'testing_intro': 'Choose a quest and summon the latest automated proof from its workflow trial. No ceremonial checklist—only witnessed evidence.',
        'testing_target': 'Quest lectern', 'testing_agent': 'Trial keeper', 'testing_run': 'Begin the trial',
        'testing_waiting': 'Awaiting a quest', 'testing_control': 'Trialmaster’s Table', 'testing_grade': 'WARD STRENGTH',
        'testing_wire': 'SCRIBE’S LIVE CHRONICLE',
        'ranking_title': 'Hall of heroes', 'ranking_eyebrow': 'GREAT HALL // GUILD ROSTER',
        'ranking_heading': 'Renown // heroes of the guild', 'ranking_live': 'LIVE GITHUB CHRONICLES',
        'registered': 'SWORN GUILD MEMBER', 'unknown': 'Unknown Wanderer', 'assets': 'GUILD TREASURY',
        'region': 'CHOOSE A HOLD', 'registry': 'Guild roster', 'record_noun': 'quests',
        'login_eyebrow': 'GUILD SEAL // GITHUB COURIER', 'login_title': 'Enter the guild through GitHub',
        'job_full': 'Grand Expedition', 'job_recon': 'Scouting Journey', 'job_back': 'QUESTS',
        'job_insertion': 'DEPARTURE', 'job_assigned': 'QUEST ACCEPTED', 'job_gates': 'Trial wards',
        'overlay_command': 'GUILD COMMAND', 'overlay_title': 'Courier ritual in progress',
        'overlay_window': 'RITUAL WINDOW', 'overlay_status': 'OPENING THE COURIER GATE',
        'overlay_timeline': 'Ritual sequence', 'overlay_first': 'Open the courier gate',
    },
}

THEME_SYNC_COPY = {
    'cyberpunk': {
        'aria': 'Uplink status', 'terminal': 'mergequest@netrunner:~', 'code': 'TTY-077',
        'prompt': 'runner@mq:~$', 'transfer': 'TRANSFER', 'sequence_label': 'AUTONOMOUS SEQUENCE',
        'stages': [
            {'title': 'Open uplink', 'detail': 'Establishing encrypted GitHub channel'},
            {'title': 'Acquire shards', 'detail': 'Fetching issue and project signals'},
            {'title': 'Decode threats', 'detail': 'Scoring priorities and assignments'},
            {'title': 'Rebuild board', 'detail': 'Preparing your refreshed contract queue'},
        ],
        'initial_log': 'Handshake request transmitted…', 'live': 'LIVE NETWORK TRANSFER', 'caution': 'DO NOT SEVER UPLINK',
        'terminal_lines': [
            '> booting MERGEQUEST / NEON OPS',
            '> loading operator profile............ OK',
            '> mounting encrypted ticket cache..... OK',
            '> negotiating with the bug oracle..... WAIT',
        ],
        'sequence': [
            {'status': 'OPENING SECURE UPLINK', 'log': 'Handshake request transmitted…'},
            {'status': 'ACQUIRING CONTRACT SHARDS', 'log': 'Receiving network signals…'},
            {'status': 'DECODING THREAT SIGNATURES', 'log': 'Normalizing priorities, owners, and project states…'},
            {'status': 'REBUILDING CONTRACT BOARD', 'log': 'Compiling the refreshed mission queue…'},
        ],
        'confirm_status': 'VERIFYING NETWORK RESPONSE', 'confirm_log': 'All contract shards received. Verifying checksum…',
        'success_status': 'BREACH COMPLETE // CONTRACT SHARDS ACQUIRED', 'failure_status': 'BREACH FAILED // ICE REJECTED UPLINK',
        'success_log': 'Uplink verified. Contract board ready.', 'failure_log': 'Network rejected the transfer. Uplink safely closed.',
        'button_active': 'Breach active…', 'button_idle': 'Breach // Resync', 'error_prefix': 'Breach failed: ',
    },
    'wwii': {
        'aria': 'Field radio status', 'terminal': 'hq@mergequest:dispatch', 'code': 'WIRE-44',
        'prompt': 'operator@field-hq:~$', 'transfer': 'RECEIVING', 'sequence_label': 'COORDINATED SEQUENCE',
        'stages': [
            {'title': 'Tune field radio', 'detail': 'Establishing secure GitHub frequency'},
            {'title': 'Receive orders', 'detail': 'Fetching issue and project dispatches'},
            {'title': 'Plot priorities', 'detail': 'Scoring priorities and assignments'},
            {'title': 'Update order board', 'detail': 'Preparing your refreshed field order queue'},
        ],
        'initial_log': 'Frequency request transmitted…', 'live': 'LIVE FIELD DISPATCH', 'caution': 'DO NOT BREAK RADIO CONTACT',
        'terminal_lines': [
            '> opening MERGEQUEST / WAR ROOM 1944',
            '> verifying operator papers........... OK',
            '> preparing field-order ledger........ OK',
            '> tuning GitHub command frequency..... WAIT',
        ],
        'sequence': [
            {'status': 'TUNING HQ FREQUENCY', 'log': 'Frequency request transmitted…'},
            {'status': 'RECEIVING FIELD ORDERS', 'log': 'Receiving field dispatches…'},
            {'status': 'PLOTTING PRIORITIES', 'log': 'Plotting priorities, owners, and project states…'},
            {'status': 'UPDATING OPERATIONS BOARD', 'log': 'Filing the refreshed assignment queue…'},
        ],
        'confirm_status': 'CONFIRMING FIELD DISPATCH', 'confirm_log': 'All field orders received. Confirming command seal…',
        'success_status': 'DISPATCH COMPLETE // FIELD ORDERS RECEIVED', 'failure_status': 'DISPATCH FAILED // HQ REJECTED ORDERS',
        'success_log': 'Radio contact confirmed. Operations board ready.', 'failure_log': 'Command rejected the dispatch. Field channel safely closed.',
        'button_active': 'Radio contact active…', 'button_idle': 'Radio HQ // Refresh', 'error_prefix': 'Dispatch failed: ',
    },
    'hacker': {
        'aria': 'Encrypted trace status', 'terminal': 'root@mergequest:~', 'code': 'TTY-ROOT',
        'prompt': 'operator@root-shell:~#', 'transfer': 'PACKETS', 'sequence_label': 'FORKED PROCESS CHAIN',
        'stages': [
            {'title': 'Open encrypted tunnel', 'detail': 'Negotiating GitHub key exchange'},
            {'title': 'Stream packets', 'detail': 'Reading issue and project payloads'},
            {'title': 'Hash signatures', 'detail': 'Scoring exposure and priority'},
            {'title': 'Commit target index', 'detail': 'Refreshing the local target cache'},
        ],
        'initial_log': 'Key exchange request transmitted…', 'live': 'LIVE ENCRYPTED TRACE', 'caution': 'DO NOT DROP CONNECTION',
        'terminal_lines': [
            '> booting MERGEQUEST / ROOT SHELL',
            '> verifying operator keypair.......... OK',
            '> mounting encrypted packet cache..... OK',
            '> opening outbound tunnel............. WAIT',
        ],
        'sequence': [
            {'status': 'PROBING GITHUB ENDPOINTS', 'log': 'Key exchange request transmitted…'},
            {'status': 'STREAMING TARGET PACKETS', 'log': 'Receiving encrypted target packets…'},
            {'status': 'HASHING PRIORITY SIGNATURES', 'log': 'Hashing owners, exposure, and project states…'},
            {'status': 'COMMITTING TARGET INDEX', 'log': 'Writing the refreshed target cache…'},
        ],
        'confirm_status': 'VERIFYING PACKET CHECKSUM', 'confirm_log': 'All target packets received. Comparing signed hashes…',
        'success_status': 'TRACE COMPLETE // TARGET INDEX UPDATED', 'failure_status': 'TRACE ABORTED // FIREWALL REJECTED TUNNEL',
        'success_log': 'Checksum verified. Target index ready.', 'failure_log': 'Remote firewall rejected the trace. Tunnel closed cleanly.',
        'button_active': 'Trace process active…', 'button_idle': 'Probe // Rescan', 'error_prefix': 'Trace failed: ',
    },
    'skyrim': {
        'aria': 'Courier ritual status', 'terminal': 'scribe@mergequest:ledger', 'code': 'RUNE-IV',
        'prompt': 'steward@great-hall:~$', 'transfer': 'SCROLLS', 'sequence_label': 'COURIER RITUAL',
        'stages': [
            {'title': 'Open the courier gate', 'detail': 'Presenting the guild seal to GitHub'},
            {'title': 'Receive quest scrolls', 'detail': 'Gathering issue and project chronicles'},
            {'title': 'Read danger sigils', 'detail': 'Weighing urgency and sworn keepers'},
            {'title': 'Renew the quest board', 'detail': 'Pinning the refreshed guild quests'},
        ],
        'initial_log': 'Guild seal sent with the courier…', 'live': 'LIVE COURIER RITUAL', 'caution': 'DO NOT BREAK THE WARD',
        'line_prefix': '⚔ ',
        'terminal_lines': [
            '⚔ Alduin descends upon the guild hall!',
            '⚔ The Dovahkiin draws steel and stands fast',
            '⚔ A Shout splits the sky — FUS RO DAH',
            '⚔ The courier rides while the battle rages…',
        ],
        'sequence': [
            {'status': 'OPENING THE COURIER GATE', 'log': 'Guild seal sent with the courier…'},
            {'status': 'RECEIVING QUEST SCROLLS', 'log': 'Gathering scrolls from the northern holds…'},
            {'status': 'READING DANGER SIGILS', 'log': 'Weighing urgency, keepers, and quest states…'},
            {'status': 'UPDATING THE GUILD BOARD', 'log': 'Pinning the renewed quest ledger…'},
        ],
        'confirm_status': 'SEALING THE COURIER DISPATCH', 'confirm_log': 'All quest scrolls received. Pressing the guild seal…',
        'success_status': 'RITUAL COMPLETE // QUEST BOARD RENEWED', 'failure_status': 'RITUAL FAILED // COURIER TURNED BACK',
        'success_log': 'The courier returned safely. The quest board is ready.', 'failure_log': 'The road was barred. The courier gate has been sealed.',
        'button_active': 'Courier on the road…', 'button_idle': 'Consult courier // Refresh', 'error_prefix': 'Courier failed: ',
    },
}

THEME_DIALECT['professional'] = {**THEME_DIALECT['cyberpunk'], **PROFESSIONAL_DIALECT}
THEME_SYNC_COPY['professional'] = {**THEME_SYNC_COPY['cyberpunk'], **PROFESSIONAL_SYNC_COPY}

THEME_SETTINGS_COPY['pvf'] = {**THEME_SETTINGS_COPY['professional'],
    'title': 'Yard settings', 'eyebrow': 'SITE OFFICE // CONFIGURATION',
    'apply': 'Save yard theme', 'root': 'Site workspace', 'local': 'Local checkout'}

THEME_DIALECT['pvf'] = {**THEME_DIALECT['professional'],
    'home_title': 'Job board', 'rank_label': 'YARD SCORE', 'rank_unit': 'YS', 'mission': 'job', 'missions': 'jobs',
    'currency': 'CR', 'currency_name': 'Yard credits', 'secured': 'Jobs handed over', 'live': 'Jobs on site',
    'fund_title': 'Your credits keep the yard running.',
    'fund_body': '25% of earned PR baseline and job rewards is routed to shared tooling, evidence storage, and recovery capacity. Balances change only when a job is handed over.',
    'incoming': 'incoming jobs', 'search_placeholder': 'Search job #1091', 'sync': 'Sync job board',
    'open_item': 'Open job', 'empty': 'NO JOBS ON THE BOARD', 'loadout': 'Site plan', 'loadout_ready': 'YARD READY',
    'targets': 'Selected jobs', 'full_strategy': 'FULL BUILD', 'recon_strategy': 'SITE SURVEY',
    'full_action': 'Start full build', 'fast_action': 'Start quick fix',
    'archive_completed': 'JOB HANDED OVER', 'archive': 'Job archive', 'accolades': 'Site milestones',
    'prompt_title': 'Prompt studio', 'testing_title': 'Site checks', 'testing_heading': 'Site inspection',
    'ranking_title': 'Crew board', 'ranking_heading': 'Yard score // crew index', 'registered': 'REGISTERED CREW MEMBER',
    'unknown': 'Unknown crew', 'assets': 'YARD CREDITS', 'region': 'SELECT SITE', 'registry': 'Crew directory', 'record_noun': 'jobs',
    'login_title': 'Connect your GitHub workspace', 'job_full': 'Full Build', 'job_recon': 'Site Survey', 'job_back': 'JOB BOARD',
    'overlay_command': 'SITE OPERATIONS', 'overlay_title': 'Raising the job board', 'overlay_status': 'SETTING UP THE SCAFFOLD',
    'overlay_timeline': 'Build sequence', 'overlay_first': 'Set the base lift'}

THEME_SYNC_COPY['pvf'] = {**THEME_SYNC_COPY['professional'],
    'aria': 'Scaffold build status', 'sequence_label': 'BUILD SEQUENCE', 'transfer': 'JOBS',
    'stages': [
        {'title': 'Set the base lift', 'detail': 'Establishing GitHub workspace channel'},
        {'title': 'Load the jobs', 'detail': 'Fetching issue and project work items'},
        {'title': 'Check the lifts', 'detail': 'Scoring priorities and assignments'},
        {'title': 'Sign off the board', 'detail': 'Preparing your refreshed job board'},
    ],
    'initial_log': 'Base lift going up…', 'live': 'LIVE SCAFFOLD BUILD', 'caution': 'KEEP THIS WINDOW OPEN',
    'terminal_lines': ['Setting up the scaffold now.', 'Connecting to GitHub… keep this window open.', 'Pulling jobs, projects, and priorities.', 'This usually takes a few seconds.'],
    'sequence': [
        {'status': 'SETTING THE BASE LIFT', 'log': 'Base lift going up…'},
        {'status': 'LOADING THE JOBS', 'log': 'Receiving project work items…'},
        {'status': 'CHECKING THE LIFTS', 'log': 'Normalizing priorities, owners, and project states…'},
        {'status': 'SIGNING OFF THE BOARD', 'log': 'Compiling the refreshed job board…'},
    ],
    'success_status': 'BUILD COMPLETE // JOB BOARD READY', 'failure_status': 'BUILD HALTED // SCAFFOLD NOT SIGNED OFF',
    'success_log': 'Scaffold signed off. Job board ready.', 'failure_log': 'Build stopped. Site left safe.',
    'button_active': 'Building…', 'button_idle': 'Sync job board', 'error_prefix': 'Build failed: '}


def current_ui_theme() -> str:
    """Return the allow-listed browser theme, falling back safely."""
    selected = request.cookies.get(THEME_COOKIE, DEFAULT_UI_THEME)
    return selected if selected in UI_THEMES else DEFAULT_UI_THEME


@app.context_processor
def inject_ui_theme() -> dict:
    selected = current_ui_theme()
    return {
        'ui_theme': selected,
        'theme_info': UI_THEMES[selected],
        'theme_settings': THEME_SETTINGS_COPY[selected],
        'theme_dialect': THEME_DIALECT[selected],
        'theme_sync': THEME_SYNC_COPY[selected],
        'ui_theme_options': UI_THEMES.values(),
        'is_wwii': selected == 'wwii',
        'is_hacker': selected == 'hacker',
        'is_skyrim': selected == 'skyrim',
    }


@app.template_filter('group_digits')
def group_digits(value: object) -> str:
    """Format whole-number HUD values with cyberpunk-style apostrophe grouping."""
    try:
        return f"{int(value):,}".replace(',', "'")
    except (TypeError, ValueError):
        return str(value)


settings = Settings()
settings.ensure_directories()
store = JobStore(settings.database_path)
runner = WorkflowRunner(settings, store)

# Resume any queued jobs from previous runs
def _resume_queued_jobs():
    for job in store.list(limit=1000):
        if job['status'] == 'queued':
            runner.start(job['id'])

_resume_queued_jobs()

def current_player_stats(jobs: list[dict]) -> dict:
    """Load the immutable PR baseline, then apply this operator's Contract rewards."""
    login = session.get('github_login')
    if not login:
        return player_stats(jobs)
    player = store.get_player(login) or {}
    if player.get('historic_prs') is None or not player.get('progression_baseline_at'):
        try:
            player = store.initialize_player_progression(
                login, fetch_historic_merged_pr_count(login),
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            # Do not lock in a zero baseline when GitHub is temporarily unavailable.
            player = player or {}
    operator_jobs = store.list_for_player(login)
    return player_stats(
        operator_jobs,
        historic_prs=player.get('historic_prs') or 0,
        baseline_at=player.get('progression_baseline_at'),
    )

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


def _github_cli_env() -> dict[str, str]:
    """Use gh's persisted credentials without an environment-token override."""
    env = os.environ.copy()
    env.pop('GH_TOKEN', None)
    env.pop('GITHUB_TOKEN', None)
    return env


def _github_cli_identity() -> dict | None:
    """Return the active GitHub CLI identity without exposing its token."""
    try:
        gh = find_gh_executable()
        env = _github_cli_env()
        token_result = subprocess.run(
            [gh, 'auth', 'token', '--hostname', 'github.com'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=5,
            env=env,
            creationflags=_subprocess_window_flags(),
        )
        token = token_result.stdout.strip()
        if token_result.returncode != 0 or not token:
            return None
        profile = subprocess.run(
            [gh, 'api', 'user'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=10,
            env={**env, 'GH_TOKEN': token},
            creationflags=_subprocess_window_flags(),
        )
        if profile.returncode != 0:
            return None
        return json.loads(profile.stdout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _connect_github_cli_user(user: dict) -> dict:
    """Attach a verified CLI identity to this browser session."""
    # A user who explicitly completes CLI authentication has selected the
    # persisted gh account over any process-level token loaded from .env.
    os.environ.pop('GH_TOKEN', None)
    os.environ.pop('GITHUB_TOKEN', None)
    session['github_auth_source'] = 'cli'
    session.pop('github_auth_detached', None)
    session['github_login'] = user['login']
    store.upsert_player(
        user['login'],
        user.get('name') or user['login'],
        user.get('avatar_url', ''),
    )
    return user


def _connect_github_cli_session() -> dict | None:
    user = _github_cli_identity()
    if not user or not user.get('login'):
        return None
    return _connect_github_cli_user(user)


def _run_cli_authentication() -> None:
    try:
        gh = find_gh_executable()
        env = _github_cli_env()
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
            encoding='utf-8',
            errors='replace',
            env=env,
            creationflags=_subprocess_window_flags(),
        )
        with _cli_auth_lock:
            _cli_auth_state['status'] = 'waiting'
            _cli_auth_state['message'] = 'Complete authorization in the GitHub browser tab.'

        output: list[str] = []
        opened_device_page = False
        if process.stdout:
            for line in process.stdout:
                clean_line = line.strip()
                if clean_line:
                    output.append(clean_line)
                    with _cli_auth_lock:
                        _cli_auth_state['output'] = output[-12:]
                    if not opened_device_page and re.search(
                        r'https://github\.com/login/device\b', clean_line, re.IGNORECASE,
                    ):
                        # gh is intentionally non-interactive here, so it prints
                        # the device URL but does not launch it itself.
                        try:
                            opened_device_page = webbrowser.open_new_tab(
                                'https://github.com/login/device',
                            )
                        except webbrowser.Error:
                            pass

        return_code = process.wait()
        user = _github_cli_identity() if return_code == 0 else None
        with _cli_auth_lock:
            if user and user.get('login'):
                _cli_auth_state['status'] = 'complete'
                _cli_auth_state['message'] = 'GitHub authorization completed.'
                _cli_auth_state['login'] = user['login']
                _cli_auth_state['name'] = user.get('name') or user['login']
                _cli_auth_state['avatar_url'] = user.get('avatar_url', '')
            elif return_code == 0:
                _cli_auth_state['status'] = 'failed'
                _cli_auth_state['message'] = (
                    'GitHub saved the authorization, but the account could not be verified. '
                    'Check your connection and try again.'
                )
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
            'message': 'Starting GitHub device authorization…',
            'output': [],
            'started_at': time.time(),
            'login': None,
            'name': None,
            'avatar_url': None,
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
    user = _connect_github_cli_session()
    if user:
        return jsonify({
            'status': 'connected',
            'login': user['login'],
            'redirect': url_for('prompts_page'),
        })
    return jsonify(_start_cli_authentication())


@app.get('/auth/cli/status')
def cli_auth_status():
    with _cli_auth_lock:
        cached_user = (
            {
                'login': _cli_auth_state['login'],
                'name': _cli_auth_state.get('name'),
                'avatar_url': _cli_auth_state.get('avatar_url', ''),
            }
            if _cli_auth_state.get('status') == 'complete' and _cli_auth_state.get('login')
            else None
        )
    if cached_user:
        user = _connect_github_cli_user(cached_user)
        return jsonify({
            'status': 'connected',
            'login': user['login'],
            'redirect': url_for('prompts_page'),
        })
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
            session['github_auth_source'] = 'oauth'
            session.pop('github_auth_detached', None)
            store.upsert_player(user.get('login', ''), user.get('name') or user.get('login', ''), user.get('avatar_url', ''))
        return redirect(url_for('prompts_page'))
    except Exception:
        return redirect(url_for('prompts_page'))


@app.get('/logout')
def logout():
    session.pop('github_token', None)
    session.pop('github_login', None)
    session.pop('github_auth_source', None)
    session['github_auth_detached'] = True
    return redirect(url_for('prompts_page'))


@app.get('/api/user')
def api_user():
    if session.get('github_login'):
        return jsonify({'login': session['github_login']})
    if session.get('github_auth_detached'):
        return jsonify({'login': None})
    token = get_github_token()
    if token:
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
    # GitHub CLI credentials are normally stored in gh's credential store.
    # Reuse them only when no explicit environment/session token takes precedence.
    user = _connect_github_cli_session()
    if user:
        return jsonify(user)
    # An anonymous dashboard visit is expected; let the frontend inspect the
    # response without producing a misleading 401 in the console.
    return jsonify({'login': None})


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


def _codex_update_enabled() -> bool:
    return not app.config.get('TESTING') or app.config.get('CODEX_UPDATE_ON_OPEN')


# Snap-installed Codex needs `sudo snap refresh` to update and has no CLI
# self-update subcommand, so the app can never keep it current unattended.
# The npm package is root-free to install/update and is what we manage here.
NPM_GLOBAL_PREFIX = Path.home() / '.npm-global'
NPM_CODEX_BIN = NPM_GLOBAL_PREFIX / 'bin' / 'codex'


def _find_codex_executable() -> str | None:
    if NPM_CODEX_BIN.is_file():
        return str(NPM_CODEX_BIN)
    candidates: list[str] = []
    for command in (settings.agent_command, settings.review_command):
        try:
            parts = shlex.split(command)
        except ValueError:
            continue
        if parts and Path(parts[0]).name == 'codex':
            candidates.append(parts[0])
    candidates.append('codex')
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.is_file():
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _codex_version(codex_bin: str) -> str:
    result = subprocess.run(
        [codex_bin, '--version'],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return (result.stdout or result.stderr).strip()


def _run_codex_update(codex_bin: str) -> None:
    with _codex_update_lock:
        _codex_update_state.update({
            'status': 'running',
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'message': 'Checking Codex CLI for updates...',
            'error': '',
        })
    try:
        before = _codex_version(codex_bin)
        with _codex_update_lock:
            _codex_update_state.update({
                'before_version': before,
                'message': 'Applying Codex CLI update if one is available...',
            })
        npm = shutil.which('npm')
        if npm and (Path(codex_bin) == NPM_CODEX_BIN or not NPM_CODEX_BIN.is_file()):
            # `codex update` is not a real CLI subcommand, and a snap-installed
            # Codex needs sudo to refresh. Manage our own npm-installed copy
            # instead, which can be kept current without elevated privileges.
            result = subprocess.run(
                [npm, 'install', '-g', '--prefix', str(NPM_GLOBAL_PREFIX), '@openai/codex@latest'],
                capture_output=True,
                text=True,
                timeout=180,
                env=os.environ.copy(),
            )
            codex_bin = str(NPM_CODEX_BIN)
        else:
            result = subprocess.run(
                [codex_bin, 'update'],
                capture_output=True,
                text=True,
                timeout=180,
                env=os.environ.copy(),
            )
        after = _codex_version(codex_bin)
        output = (result.stdout or result.stderr).strip()
        if result.returncode == 0:
            changed = before and after and before != after
            message = f'Codex CLI updated: {before} -> {after}' if changed else 'Codex CLI is already up to date.'
            if output and not changed:
                message = output.splitlines()[-1][:240]
            with _codex_update_lock:
                _codex_update_state.update({
                    'status': 'complete',
                    'after_version': after,
                    'completed_at': datetime.now(timezone.utc).isoformat(),
                    'message': message,
                })
        else:
            with _codex_update_lock:
                _codex_update_state.update({
                    'status': 'failed',
                    'after_version': after,
                    'completed_at': datetime.now(timezone.utc).isoformat(),
                    'message': 'Codex CLI update failed; continuing with the installed version.',
                    'error': output[:1000] or f'codex update exited with {result.returncode}',
                })
    except Exception as exc:
        with _codex_update_lock:
            _codex_update_state.update({
                'status': 'failed',
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'message': 'Codex CLI update check failed; continuing with the installed version.',
                'error': str(exc),
            })


def _ensure_codex_update_started() -> dict:
    with _codex_update_lock:
        if _codex_update_state['started']:
            return dict(_codex_update_state)
        _codex_update_state['started'] = True
    codex_bin = _find_codex_executable()
    if not codex_bin:
        with _codex_update_lock:
            _codex_update_state.update({
                'status': 'skipped',
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'message': 'Codex CLI was not found on PATH; skipping update check.',
            })
            return dict(_codex_update_state)
    thread = threading.Thread(target=_run_codex_update, args=(codex_bin,), daemon=True)
    thread.start()
    with _codex_update_lock:
        return dict(_codex_update_state)


def _wait_for_codex_update(timeout: int = 60) -> dict:
    """Block until Codex update completes or timeout expires."""
    start = time.time()
    while time.time() - start < timeout:
        with _codex_update_lock:
            state = dict(_codex_update_state)
        if state['status'] in {'complete', 'failed', 'skipped'}:
            return state
        time.sleep(0.5)
    return dict(_codex_update_state)


@app.get('/codex-update')
def codex_update_page():
    if not _codex_update_enabled():
        return redirect(url_for('index'))
    state = _ensure_codex_update_started()
    return render_template('codex_update.html', update=state, next_url=request.args.get('next') or url_for('index'))


@app.get('/api/codex-update')
def api_codex_update():
    if not _codex_update_enabled():
        return jsonify({'status': 'skipped', 'message': 'Codex update check disabled.'})
    return jsonify(_ensure_codex_update_started())


@app.get('/')
def home():
    return redirect(url_for('index'))


# Curated shortlist of free OpenCode models, ranked by SWE-bench Pro score (where
# published) from vendor/benchmark reports. models.dev has no benchmark data of its
# own, so this list is maintained by hand rather than derived from the live catalog.
CURATED_MODELS = [
    {'rank': 1, 'id': 'kenari/claude-fable-5', 'name': 'Claude Fable 5', 'context': 1000000, 'swe_score': 80.0, 'notes': 'Frontier coding / agentic model'},
    {'rank': 2, 'id': 'kenari/claude-opus-5', 'name': 'Claude Opus 5', 'context': 1000000, 'swe_score': 79.2, 'notes': 'Hard repo-level coding and autonomous agent work'},
    {'rank': 3, 'id': 'kenari/claude-opus-4-8', 'name': 'Claude Opus 4.8', 'context': 1000000, 'swe_score': 69.2, 'notes': 'Strong repo-level coding'},
    {'rank': 4, 'id': 'alibaba-token-plan/qwen3.8-max', 'name': 'Qwen3.8 Max', 'context': 1000000, 'swe_score': 67.7, 'notes': 'Large-context coding / agentic model'},
    {'rank': 5, 'id': 'kenari/gpt-5-6-sol', 'name': 'GPT-5.6 Sol', 'context': 1050000, 'swe_score': 64.6, 'notes': 'General coding, reasoning and tool use'},
    {'rank': 6, 'id': 'kenari/gpt-5-6-terra', 'name': 'GPT-5.6 Terra', 'context': 1050000, 'swe_score': 63.4, 'notes': 'General coding and agentic work'},
    {'rank': 7, 'id': 'kenari/claude-sonnet-5', 'name': 'Claude Sonnet 5', 'context': 1000000, 'swe_score': 63.2, 'notes': 'Strong daily-driver coding agent'},
    {'rank': 8, 'id': 'kenari/gpt-5-6-luna', 'name': 'GPT-5.6 Luna', 'context': 1050000, 'swe_score': 62.7, 'notes': 'General coding and agentic work'},
    {'rank': 9, 'id': 'alibaba-token-plan/glm-5.2', 'name': 'GLM-5.2', 'context': 1000000, 'swe_score': 62.1, 'notes': 'Coding and long-context agent work'},
    {'rank': 10, 'id': 'ovhcloud/qwen3.8-27b', 'name': 'Qwen3.8 27B', 'context': 262000, 'swe_score': 61.7, 'notes': 'Smaller Qwen coding option'},
    {'rank': 11, 'id': 'alibaba-token-plan/qwen3.7-max', 'name': 'Qwen3.7 Max', 'context': 1000000, 'swe_score': 60.6, 'notes': 'Large-context coding model'},
    {'rank': 12, 'id': 'poolside/poolside/laguna-s-2.1', 'name': 'Laguna S 2.1', 'context': 1048576, 'swe_score': 59.4, 'notes': 'Software-engineering focused'},
    {'rank': 13, 'id': 'kenari/gpt-5-5', 'name': 'GPT-5.5', 'context': 1050000, 'swe_score': 59.4, 'notes': 'General coding / agentic model'},
    {'rank': 14, 'id': 'kenari/minimax-m3', 'name': 'MiniMax M3', 'context': 1000000, 'swe_score': 59.0, 'notes': 'Long-context coding option'},
    {'rank': 15, 'id': 'kenari/minimax-m2-7', 'name': 'MiniMax M2.7', 'context': 204800, 'swe_score': 56.2, 'notes': 'Lower-cost coding option'},
    {'rank': 16, 'id': 'alibaba-token-plan/deepseek-v4-pro-0813', 'name': 'DeepSeek V4 Pro', 'context': 1000000, 'swe_score': 55.4, 'notes': 'Reasoning-heavy coding model'},
    {'rank': 17, 'id': 'kenari/gemini-3-1-pro', 'name': 'Gemini 3.1 Pro', 'context': 1048000, 'swe_score': 54.2, 'notes': 'Large-codebase analysis and coding'},
    {'rank': 18, 'id': 'alibaba-token-plan/deepseek-v4-flash-0731', 'name': 'DeepSeek V4 Flash', 'context': 1000000, 'swe_score': 52.6, 'notes': 'Published max-reasoning configuration'},
    {'rank': 19, 'id': 'umans-ai-coding-plan/umans-qwen3.6-35b-a3b', 'name': 'Qwen3.6 35B-A3B', 'context': 262000, 'swe_score': 51.2, 'notes': 'MoE coding / agentic model'},
    {'rank': 20, 'id': 'alibaba-coding-plan-cn/qwen3-coder-next', 'name': 'Qwen3-Coder Next', 'context': 262000, 'swe_score': 70.6, 'notes': 'Dedicated coding model; SWE-bench Verified, not directly comparable with Pro'},
    {'rank': 21, 'id': 'kenari/kimi-k3', 'name': 'Kimi K3', 'context': 1000000, 'swe_score': None, 'notes': 'Long-horizon coding model; no clean comparable SWE-bench Pro score'},
    {'rank': 22, 'id': 'alibaba-token-plan/kimi-k2.7-code', 'name': 'Kimi K2.7 Code', 'context': 262000, 'swe_score': None, 'notes': 'Dedicated coding model'},
    {'rank': 23, 'id': 'cohere/north-mini-code-1-0', 'name': 'North Mini Code', 'context': 256000, 'swe_score': None, 'notes': 'Dedicated coding model'},
    {'rank': 24, 'id': 'mistral/labs-devstral-small-2512', 'name': 'Devstral Small 2', 'context': 256000, 'swe_score': None, 'notes': 'Agentic software-engineering model'},
]


def _parse_opencode_models_verbose(output: str) -> dict[str, dict]:
    """Parse `opencode models <provider> --verbose` output into id -> metadata.

    Each entry is a bare "provider/model-id" line followed by a JSON object
    describing that model (cost, context limits, variants). Brace-count the
    JSON block rather than assuming one-line-per-field formatting.
    """
    entries: dict[str, dict] = {}
    lines = output.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if '/' not in line or line.startswith('{') or line.startswith('}'):
            index += 1
            continue
        model_id = line
        index += 1
        json_lines = []
        depth = 0
        started = False
        while index < len(lines):
            json_lines.append(lines[index])
            depth += lines[index].count('{') - lines[index].count('}')
            started = started or '{' in lines[index]
            index += 1
            if started and depth <= 0:
                break
        try:
            entries[model_id] = json.loads('\n'.join(json_lines))
        except json.JSONDecodeError:
            continue
    return entries


@app.get('/api/opencode/models')
def opencode_models():
    """Return the free models reported by the local OpenCode CLI.

    OpenCode owns the provider model cache, so querying it is more reliable than
    maintaining a second catalogue (or depending on the external models.dev API).
    Keep the curated catalogue as an offline fallback for installations without
    the CLI or when its refresh fails.
    """
    models = []
    try:
        completed = subprocess.run(
            ['opencode', 'models', 'opencode', '--verbose', '--refresh'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0:
            entries = _parse_opencode_models_verbose(completed.stdout)
            free_ids = sorted(
                model_id for model_id, data in entries.items()
                if (data.get('cost') or {}).get('input', 1) == 0
                and (data.get('cost') or {}).get('output', 1) == 0
            )
            models = [
                {
                    'rank': rank,
                    'id': model_id,
                    'name': entries[model_id].get('name') or model_id.rsplit('/', 1)[-1].replace('-', ' ').title(),
                    'context': (entries[model_id].get('limit') or {}).get('context'),
                    'swe_score': None,
                    'notes': 'Free model reported by OpenCode',
                    'variants': list((entries[model_id].get('variants') or {}).keys()),
                }
                for rank, model_id in enumerate(free_ids, 1)
            ]
    except (OSError, subprocess.SubprocessError):
        pass
    if not models:
        models = [
            {**item, 'variants': []}
            for item in sorted(CURATED_MODELS, key=lambda entry: entry['rank'])
        ]
    return jsonify({'models': models})


@app.get('/jobs')
def index():
    if _codex_update_enabled():
        update = _ensure_codex_update_started()
        if update.get('status') in {'idle', 'running'}:
            return render_template('codex_update.html', update=update, next_url=request.full_path)
    # The archive renders only 30 rows; avoid decoding hundreds of unused jobs.
    jobs = store.list(limit=30)
    watch_ids = [item for item in request.args.get('watch', '').split(',') if item]
    watched_jobs = [store.get(job_id) for job_id in watch_ids]
    watched_jobs = [job for job in watched_jobs if job]
    tickets = store.list_tickets()
    return render_template('index.html', jobs=jobs, tickets=tickets, project_boards=(7, 11), defaults=settings, player=current_player_stats(jobs), watched_jobs=watched_jobs)

@app.get('/leaderboard')
def leaderboard_page():
    jobs = store.list(limit=500)
    current = current_player_stats(jobs)
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

@app.get('/stats/stages')
def stage_timing_stats():
    """Per-stage count/avg/median/slowest duration_ms across all delivery jobs."""
    return jsonify(store.get_stage_timing_report())

@app.get('/settings')
def settings_page():
    return render_template('settings.html', settings=settings)


@app.post('/settings/clear-queue')
def clear_queue():
    cleared = store.clear_queue()
    return redirect(url_for('settings_page', queue_cleared=cleared))


@app.post('/settings/demo-notification')
def demo_notification():
    """Trigger the same local notification used for completed pull requests."""
    if not shutil.which('powershell.exe') and not shutil.which('notify-send'):
        return redirect(url_for('settings_page', notification='unavailable'))
    sent = []
    demo_ticket_url = 'https://github.com/pvfscaffolding/crm-staff-desktop/issues/898'
    WorkflowRunner._send_windows_notification(
        'MergeQuest demo notification',
        'Local notifications are working. Click to open ticket #898.',
        sent.append,
        launch_url=demo_ticket_url,
    )
    if sent and sent[0].endswith('notification sent.'):
        return redirect(url_for('settings_page', notification='sent'))
    return redirect(url_for('settings_page', notification='failed'))


@app.post('/settings/theme')
def update_theme():
    selected = request.form.get('theme', '')
    if selected not in UI_THEMES:
        return render_template(
            'settings.html',
            settings=settings,
            theme_error='Choose one of the available interface themes.',
        ), 400
    response = redirect(url_for('settings_page', saved=selected))
    response.set_cookie(
        THEME_COOKIE,
        selected,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite='Lax',
        path='/',
    )
    return response


@app.post('/jobs')
def create_job():
    form = request.form
    issue_urls = [url.strip() for url in form.getlist('issue_url') if url.strip()]
    if not issue_urls:
        issue_urls = [form.get('issue_url', '').strip()]
    issue_urls = list(dict.fromkeys(issue_urls))
    if len(issue_urls) > 3:
        return jsonify({'error': 'Select no more than 3 tickets at a time'}), 400
    ticket_rewards = {
        ticket['url'].strip().rstrip('/'): CONTRACT_REWARDS.get(
            ticket.get('priority'), DEFAULT_CONTRACT_REWARD,
        )
        for ticket in store.list_tickets(state='')
    }
    parameters = {
        'issue_url': issue_urls[0] if issue_urls else '',
        'base_branch': form.get('base_branch', 'develop').strip(),
        'branch_prefix': form.get('branch_prefix', 'feature').strip(),
        'agent_command': form.get('agent_command', '').strip(),
        'review_command': form.get('review_command', '').strip(),
        'agent_provider': form.get('agent_provider', 'codex'),
        'review_provider': form.get('review_provider', 'codex'),
        'model_auto_escalate': form.get('model_auto_escalate') == 'on',
        'validation_commands': form.get('validation_commands', ''),
        'close_issue_on_merge': form.get('close_issue_on_merge') == 'on',
        'comment_on_failure': form.get('comment_on_failure') == 'on',
        'approval_mode': 'each_stage' if form.get('workflow_profile') == 'manual' else form.get('approval_mode', 'auto'),
        'workflow_profile': form.get('workflow_profile', 'full_pr'),
        'github_login': session.get('github_login'),
    }
    if not parameters['issue_url']:
        jobs = store.list(limit=30)
        return render_template('index.html', jobs=jobs, tickets=store.list_tickets(), defaults=settings, player=current_player_stats(jobs), form=form, form_error='Issue URL required'), 400
    job_ids = []
    # Ensure Codex is updated before starting any jobs
    if parameters.get('agent_provider') == 'codex' or parameters.get('review_provider') == 'codex':
        _ensure_codex_update_started()
        _wait_for_codex_update(timeout=120)
    for issue_url in issue_urls:
        job_parameters = {
            **parameters,
            'issue_url': issue_url,
            'contract_reward': ticket_rewards.get(
                issue_url.rstrip('/'), DEFAULT_CONTRACT_REWARD,
            ),
        }
        job_id = store.create(job_parameters)
        runner.start(job_id)
        job_ids.append(job_id)
    if len(job_ids) > 1:
        return redirect(url_for('index', watch=','.join(job_ids)))
    return redirect(url_for('job_detail', job_id=job_ids[0]))


@app.get('/jobs/<job_id>')
def job_detail(job_id):
    if hasattr(store, 'get_with_logs'):
        try:
            job = store.get_with_logs(job_id)
        except TypeError:
            # Test/integration stores may expose the legacy one-argument getter.
            job = store.get(job_id)
    else:
        job = store.get(job_id)
    if not job:
        return 'Job not found', 404
    return render_template('job.html', job=job)


@app.get('/api/jobs/<job_id>')
def api_job(job_id):
    cursor_value = request.args.get('log_cursor')
    if cursor_value is not None and hasattr(store, 'get_updates'):
        try:
            cursor = max(0, int(cursor_value))
        except ValueError:
            cursor = 0
        job = store.get_updates(job_id, cursor)
    elif request.args.get('log_offset') is not None and hasattr(store, 'get_with_logs'):
        job = store.get_with_logs(job_id)
    else:
        job = store.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    offset_value = request.args.get('log_offset')
    if offset_value is not None:
        logs = job.pop('logs', '') or ''
        try:
            offset = max(0, int(offset_value))
        except ValueError:
            offset = 0
        # An offset beyond the current log means the client cursor is stale;
        # send a replacement snapshot instead of silently losing telemetry.
        if offset > len(logs):
            offset = 0
            job['logs_reset'] = True
        job['logs_delta'] = logs[offset:]
        job['log_offset'] = len(logs)
    return jsonify(job), 200


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


@app.post('/api/jobs/<job_id>/restart')
def restart_job(job_id):
    job = store.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'queued':
        return jsonify({'error': 'Only queued jobs can be restarted'}), 409
    runner.start(job_id)
    return jsonify(store.get(job_id))


@app.post('/api/jobs/<job_id>/resume')
def resume_job(job_id):
    """Force a stuck job back to queued state and start it."""
    job = store.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    # Transition to queued and immediately start (skip waiting in queue)
    store.update(job_id, status='queued')
    runner.start(job_id)
    return jsonify(store.get(job_id))


@app.get("/")
def root():
    return redirect(url_for("index"))


@app.get("/prompts")
def prompts_page():
    return render_template('prompts.html')


@app.get("/testing")
def testing_page():
    return render_template(
        'testing.html',
        tickets=store.list_testing_tickets(),
        repositories=store.list_ticket_repositories(),
        ticket_references=store.list_ticket_references(),
    )


@app.get("/tickets/lookup")
def lookup_ticket():
    """Resolve a bare ticket number against every known repository live via
    GitHub. Covers tickets that are closed or otherwise never synced into the
    local index, where a plain number alone is ambiguous without this."""
    number = request.args.get('number', '').strip()
    if not number.isdigit():
        return jsonify({'error': 'A numeric ticket number is required.'}), 400
    candidate_repos = sorted({*store.list_ticket_repositories(), *CONTRACT_REPOSITORIES})
    matches = []
    for repository in candidate_repos:
        try:
            issue = fetch_issue_from_github(f'https://github.com/{repository}/issues/{number}')
        except Exception:
            continue
        if issue.get('pull_request'):
            continue
        matches.append({
            'repository': repository,
            'number': int(number),
            'title': issue.get('title', ''),
            'state': issue.get('state', ''),
            'url': f'https://github.com/{repository}/issues/{number}',
        })
    return jsonify({'matches': matches})


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
    # Evidence Review always runs the same single escalation pipeline
    # (Haiku -> Sonnet -> Luna -> Opus -> Sol Low -> Sol High), matching the Testing Lab gauge.
    # The agent is not user-selectable for this workflow.
    payload = request.get_json(silent=True) or {}
    issue_url = str(payload.get('issue_url', '')).strip()
    if not issue_url:
        return jsonify({'error': 'GitHub issue URL required'}), 400
    try:
        parse_github_url(issue_url)
        job_id = store.create({
            'issue_url': issue_url, 'base_branch': 'develop',
            'workflow_profile': 'testing_only', 'agent_provider': 'claude',
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
    # Same fixed pipeline as Evidence Review: not user-selectable.
    fix_id = store.create({
        'issue_url': source['issue_url'], 'base_branch': 'develop',
        'workflow_profile': 'qa_fix', 'agent_provider': 'claude',
        'review_provider': 'claude', 'agent_command': '', 'review_command': '',
        'branch_prefix': 'feature', 'validation_commands': '',
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
    # failure prevents the ticket from taking its successful QA transition.
    overall = 'failed' if 'failed' in statuses else 'passed'
    result['tests_run'] = tests
    result['overall'] = overall
    result['override_allowed'] = True
    result.setdefault('automated_overall', source.get('result', {}).get('automated_overall') or source.get('result', {}).get('overall'))
    project_warning = ''
    if overall == 'passed':
        try:
            project_ops = GitHubOps(settings.command_timeout_seconds, lambda message: None)
            issue_ref = parse_issue_url(source['issue_url'])
            result['project_status'] = project_ops.sync_successful_qa_project_fields(
                issue_ref, result.get('repositories') or [issue_ref.repo],
            )
            if not result['project_status']['updated']:
                if result['project_status']['has_open_pr']:
                    project_warning = (
                        'Override saved, but the project is missing a PR Ready Status '
                        'or Test State field.'
                    )
                else:
                    project_warning = (
                        'Override saved, but the project is missing a Done Status '
                        'or Pass Test State option.'
                    )
        except Exception as exc:
            project_warning = f'Override saved, but GitHub project fields could not update: {exc}'
            result['project_status'] = {
                'updated': False, 'count': 0, 'test_state_count': 0,
                'status': None, 'test_state': None, 'warning': str(exc),
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
        requested = payload.get('repositories')
        project_number = payload.get('project_number')
        if project_number is not None:
            try:
                project_number = int(project_number)
            except (TypeError, ValueError) as exc:
                raise ValueError('Project board must be a number.') from exc
            if project_number not in ALLOWED_PROJECT_NUMBERS:
                raise ValueError('Project board is not supported.')
        if requested is not None and not isinstance(requested, list):
            raise ValueError('Repositories must be provided as a list.')
        repositories = (
            [str(repository).strip() for repository in requested if str(repository).strip()]
            if isinstance(requested, list)
            else [str(payload.get('repository') or '').strip()]
        )
        repositories = list(dict.fromkeys(repository for repository in repositories if repository))
        if not repositories:
            repositories = list(CONTRACT_REPOSITORIES)
        token = get_github_token()
        synced = []
        for repository in repositories:
            repository_tickets = sync_github(repository, token=token)
            store.prune_repository_tickets(
                repository, [ticket['key'] for ticket in repository_tickets],
            )
            store.upsert_tickets(repository_tickets)
            synced.extend(repository_tickets)
        eligible = store.list_tickets()
        eligible = [
            ticket for ticket in eligible
            if ticket.get('repository') in repositories
        ]
        return jsonify({
            'count': len(eligible),
            'synced_count': len(synced),
            'repositories': repositories,
        })
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
    branch_prefix = request.json.get("branch_prefix", "feature").strip()

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
    branch_prefix = request.json.get("branch_prefix", "feature").strip()
    if not issue_url:
        return jsonify({"error": "issue_url required"}), 400
    try:
        issue = fetch_issue_from_github(issue_url)
        branch_name = make_branch_name(branch_prefix, issue.get("number"), issue.get("title", ""))
        return jsonify({"prompt": all_in_one_prompt(issue, base_branch, branch_name)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    raw_port = os.getenv("APP_PORT", "").strip()
    try:
        start_port = int(raw_port) if raw_port else 3060
        if not (1 <= start_port <= 65535):
            raise ValueError
    except ValueError:
        start_port = 3060
    port = find_available_port(start_port=start_port)
    host = os.getenv("APP_HOST", "127.0.0.1")
    print(f"Starting on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
