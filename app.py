from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from core import make_branch_name
from config import Settings
from prompts import investigation_prompt, review_prompt
from store import JobStore
from ticket_sync import import_workbook, sync_github

HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Ticket PR Agent</title>
    <style>
        :root { --ink:#f4f7ff; --muted:#8fa1be; --line:#28405f; --panel:#0d1d36; --panel-2:#09172c; --bg:#030b19; --blue:#4d9dff; --cyan:#4de6d2; --green:#65e2bd; }
        * { box-sizing:border-box; }
        body { margin:0; min-height:100vh; background:radial-gradient(circle at 82% 9%,#102c5f 0,transparent 24%),radial-gradient(circle at 18% 40%,#0c2c58 0,transparent 32%),linear-gradient(180deg,#040b18,#06152b 68%,#071a36); color:var(--ink); font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
        body:before { content:""; position:fixed; inset:0; pointer-events:none; opacity:.17; background-image:linear-gradient(#4a70a51a 1px,transparent 1px),linear-gradient(90deg,#4a70a51a 1px,transparent 1px); background-size:64px 64px; mask-image:linear-gradient(to bottom,#000,transparent 76%); }
        .shell { position:relative; max-width:1480px; margin:0 auto; padding:0 46px 64px; }
        .topbar { display:flex; align-items:center; justify-content:space-between; min-height:92px; border-bottom:1px solid #263d5d; margin-bottom:48px; }
        .brand { display:flex; align-items:center; gap:11px; color:#fff; font-weight:750; letter-spacing:-.02em; font-size:17px; }
        .brand-mark { display:grid; place-items:center; width:38px; height:38px; border:1px solid #7598ff99; border-radius:9px; color:#fff; background:linear-gradient(145deg,#1762c0,#6339c6); box-shadow:0 0 26px #326eff55,inset 0 1px #ffffff44; }
        .topbar-badge { padding:9px 14px; border:1px solid #216a6488; border-radius:999px; color:#78e8cf; background:#0b302fbb; font-size:11px; font-weight:700; letter-spacing:.02em; }
        .topbar-badge:before { content:"♙"; margin-right:7px; }
        .hero { position:relative; min-height:265px; margin-bottom:30px; padding:4px 42% 20px 24px; }
        .eyebrow { color:var(--cyan); font-size:11px; text-transform:uppercase; letter-spacing:.2em; font-weight:850; margin:0 0 23px; }
        .eyebrow:after { content:""; display:inline-block; width:25px; height:2px; margin-left:13px; vertical-align:middle; background:var(--cyan); }
        h1 { max-width:760px; margin:0; color:#f7f8fc; font-size:clamp(40px,4.4vw,66px); letter-spacing:-.045em; line-height:1.05; }
        h1 span { color:var(--cyan); text-shadow:0 0 28px #3bd9ce55; }
        .hero-copy { color:#a0aec4; font-size:15px; line-height:1.65; margin:21px 0 0; max-width:670px; }
        .hero-art { position:absolute; right:3%; top:-8px; width:400px; height:270px; overflow:visible; filter:drop-shadow(0 18px 30px #00163f88); }
        .hero-art:not(.hero-art-image) { display:none; }
        .hero-art-image { object-fit:contain; filter:drop-shadow(0 18px 30px #00163f88); }
        .hero-art .orbit { fill:none; stroke:#4589f2; stroke-width:1; opacity:.35; }
        .hero-art .wire { fill:none; stroke:#3c85ed; stroke-width:1.2; stroke-linecap:round; opacity:.55; }
        .hero-art .wire.ai { stroke:#4de6d2; opacity:.82; }
        .hero-art .brain { fill:#eaf2ff0d; stroke:#dbe8ff; stroke-width:2.3; stroke-linecap:round; stroke-linejoin:round; }
        .hero-art .brain-fold { fill:none; stroke:#b7ceff; stroke-width:1.5; stroke-linecap:round; opacity:.76; }
        .hero-art .brain-fold.ai { stroke:#58e9d5; }
        .hero-art .github { fill:#f7f9ff; }
        .hero-art .node { fill:#d9ffff; stroke:#4de6d2; stroke-width:1.7; }
        .layout { display:grid; grid-template-columns:minmax(0,1fr); gap:24px; align-items:start; }
        .panel { background:linear-gradient(145deg,#10233fdd,#0a1931e8); border:1px solid #2d4769; border-radius:15px; box-shadow:0 26px 70px #00081966,inset 0 1px #ffffff09; overflow:hidden; }
        .panel-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; padding:25px 28px 19px; }
        .panel-title { display:flex; gap:14px; align-items:flex-start; }
        .panel-icon { display:grid; place-items:center; flex:0 0 36px; height:36px; border:1px solid #526dff88; border-radius:9px; color:#d9e6ff; background:linear-gradient(145deg,#215aa5,#5732c2); box-shadow:0 0 20px #375ed044; }
        h2 { margin:0; color:#f0f6ff; font-size:16px; letter-spacing:-.02em; }
        .section-copy { color:var(--muted); font-size:12px; line-height:1.5; margin:5px 0 0; }
        .panel-body { padding:0 28px 28px; }
        .field { display:block; color:#aab8ce; font-size:11px; font-weight:750; letter-spacing:.04em; text-transform:uppercase; margin-bottom:16px; }
        input { display:block; width:100%; margin-top:8px; padding:14px 15px; border:1px solid #2c496c; border-radius:7px; background:#07152aaa; color:#edf4ff; font:inherit; font-size:13px; outline:none; transition:border .15s,box-shadow .15s,background .15s; }
        input::placeholder { color:#566982; }
        input:focus { border-color:#4b9fff; box-shadow:0 0 0 3px #398cf52b, 0 0 22px #398cf514; background:#0a172b; }
        select { display:block; width:100%; margin-top:8px; padding:12px 13px; border:1px solid #2c3e5c; border-radius:8px; background:#091323aa; color:#edf4ff; font:inherit; font-size:13px; outline:none; }
        .form-grid { display:grid; grid-template-columns:1fr 1fr; gap:18px 26px; }
        .form-grid .wide { grid-column:1/-1; }
        .actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:4px; }
        .queue-actions { justify-content:flex-end; margin:-4px 0 18px; }
        button { border:1px solid #335374; border-radius:7px; padding:11px 16px; background:#10213b; color:#b9c8dc; cursor:pointer; font:inherit; font-size:12px; font-weight:750; transition:transform .12s,border .12s,box-shadow .12s,background .12s; }
        button:hover { transform:translateY(-1px); border-color:#5784b8; box-shadow:0 7px 18px #00000027; }
        button.primary { color:#061326; border-color:#65d8ce; background:linear-gradient(135deg,#6de2d2,#55bff5); box-shadow:0 7px 20px #4bd6d02c; }
        button.primary:hover { background:linear-gradient(135deg,#8aeee2,#70c8ff); }
        button.subtle { color:#b9c8dc; background:#0f1d32; }
        .sync-meta { color:#7487a4; font-size:11px; margin:-5px 0 0; }
        #ticketStatus { min-height:14px; margin:5px 0 9px; color:var(--green); font-size:11px; font-weight:700; }
        .job-status { min-height:22px; margin-top:18px; color:#9ab2d0; font-size:11px; line-height:1.5; }
        .table-wrap { overflow-x:auto; border:1px solid #315071; border-radius:10px; }
        table { width:100%; border-collapse:collapse; table-layout:auto; font-size:11px; }
        th { padding:12px 12px; text-align:left; color:#8ca0be; background:#0a172b; border-bottom:1px solid #315071; border-right:1px solid #223b59; font-size:9px; text-transform:uppercase; letter-spacing:.06em; white-space:nowrap; }
        td { padding:10px 12px; border-bottom:1px solid #25415f; border-right:1px solid #223b59; vertical-align:middle; color:#9fb1c9; }
        tr:last-child td { border-bottom:0; }
        tr:hover td { background:#17294488; }
        td:nth-child(2) { font-weight:800; color:var(--blue); }
        td:nth-child(5) { min-width:250px; color:#e8f0fb; font-weight:600; }
        td:nth-child(6), td:nth-child(7) { color:#8799b3; }
        a { color:inherit; text-decoration:none; }
        td:nth-child(2) a:hover { text-decoration:underline; }
        .chip { display:inline-flex; align-items:center; min-height:25px; padding:4px 9px; border:1px solid #304b6b; border-radius:6px; color:#9fb2ca; background:#10223b; white-space:nowrap; }
        .priority-p0,.priority-p1 { color:#ffdce3; border-color:#a5324e; background:#641d36aa; }
        .priority-p2 { color:#ffeac0; border-color:#946b17; background:#5a400faa; }
        .status-triage { color:#8be9db; border-color:#28756e; background:#164b4a88; }
        .assignee { display:inline-flex; align-items:center; gap:7px; }
        .avatar { display:grid; place-items:center; width:24px; height:24px; border-radius:50%; background:linear-gradient(145deg,#435c85,#273955); color:#dbe8f9; font-size:9px; }
        .use-ticket { padding:8px 11px; color:#79e7d3; border-color:#286d6b; background:#0b2535; white-space:nowrap; }
        .output { display:none; background:#080f1d; color:#dbeafe; padding:20px; margin-top:20px; white-space:pre-wrap; border:1px solid #263b5c; border-radius:12px; overflow-y:auto; max-height:600px; font:12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; box-shadow:0 20px 50px #00000033; }
        #copyBtn { margin-top:10px; }
        .empty { color:var(--muted); text-align:center; padding:30px 15px; }
        @media (max-width:900px) { .shell { padding:0 14px 44px; } .topbar { min-height:72px; margin-bottom:32px; } .hero { padding:0 4px 20px; min-height:0; } .hero-art { display:none; } .panel-head,.panel-body { padding-left:17px; padding-right:17px; } .form-grid { grid-template-columns:1fr; gap:0; } .form-grid .wide { grid-column:auto; } table { min-width:930px; } }
    </style>
</head>
<body>
<main class="shell">
    <header class="topbar">
        <div class="brand"><span class="brand-mark">✓</span> Ticket PR Agent</div>
        <span class="topbar-badge">Human approval required</span>
    </header>
    <section class="hero">
        <p class="eyebrow">GitHub issue triage → reviewed fix</p>
        <h1>Turn the next important ticket into <span>progress.</span></h1>
        <p class="hero-copy">Sync your GitHub queue, choose a ticket, and generate a focused investigation or review prompt with the right branch settings already in place.</p>
        <svg class="hero-art" aria-hidden="true" viewBox="0 0 400 270" role="img">
            <defs>
                <linearGradient id="tileTop" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#335d9f"/><stop offset=".5" stop-color="#182d58"/><stop offset="1" stop-color="#102347"/></linearGradient>
                <linearGradient id="tileEdge" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#3178ec"/><stop offset=".55" stop-color="#5135df"/><stop offset="1" stop-color="#162b75"/></linearGradient>
                <radialGradient id="tileShine" cx="35%" cy="25%"><stop stop-color="#669dff" stop-opacity=".32"/><stop offset="1" stop-color="#16274b" stop-opacity="0"/></radialGradient>
                <filter id="glow"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
            </defs>
            <ellipse class="orbit" cx="200" cy="146" rx="174" ry="87" stroke-dasharray="5 10"/>
            <ellipse class="orbit" cx="200" cy="147" rx="143" ry="66"/>
            <path class="wire" d="M37 98h47l24 16M31 179h58l22-14M292 111l28-20h47M291 166l31 20h48"/>
            <path class="wire ai" d="M55 68h32l19 17M315 70h27v-18h25"/>
            <g filter="url(#glow)"><circle class="node" cx="37" cy="98" r="3.5"/><circle class="node" cx="31" cy="179" r="3.5"/><circle class="node" cx="367" cy="91" r="3.5"/><circle class="node" cx="370" cy="186" r="3.5"/><circle class="node" cx="55" cy="68" r="3"/><circle class="node" cx="367" cy="52" r="3"/></g>
            <path d="M106 132l78-47q16-10 32 0l78 47q13 8 0 16l-78 47q-16 10-32 0l-78-47q-13-8 0-16z" fill="#172450" opacity=".85" transform="translate(0 22)"/>
            <path d="M106 132l78-47q16-10 32 0l78 47q13 8 0 16l-78 47q-16 10-32 0l-78-47q-13-8 0-16z" fill="url(#tileEdge)" transform="translate(0 13)" stroke="#5a72ff" stroke-width="1"/>
            <path d="M106 126l78-47q16-10 32 0l78 47q13 8 0 16l-78 47q-16 10-32 0l-78-47q-13-8 0-16z" fill="url(#tileTop)" stroke="#78a8ff" stroke-width="1.4"/>
            <path d="M106 126l78-47q16-10 32 0l78 47q13 8 0 16l-78 47q-16 10-32 0l-78-47q-13-8 0-16z" fill="url(#tileShine)"/>
            <g transform="translate(0 -2)">
                <path class="brain" d="M199 98c-12-13-33-12-42 2-15-1-26 11-24 25-12 8-9 26 4 31 0 15 15 24 29 18 10 12 27 11 35 0 9 10 26 7 32-6 15 0 24-16 16-29 6-15-4-31-20-31-6-11-18-14-30-10z"/>
                <path class="brain-fold" d="M194 103c-10 5-11 14-5 20-10 5-9 14-2 19-8 6-5 15 1 19M174 104c-9 4-12 12-7 19-9 4-10 13-3 19"/>
                <path class="brain-fold ai" d="M207 103c10 5 12 14 6 21 9 4 10 13 4 19 7 6 4 14-2 18M226 111c8 5 8 14 2 20 7 6 4 14-3 18"/>
                <g transform="translate(149 119) scale(.23)" class="github"><path d="M100 0C44.8 0 0 44.8 0 100c0 44.2 28.7 81.7 68.5 94.9 5 .9 6.8-2.2 6.8-4.8v-18.1c-27.9 6.1-33.8-13.5-33.8-13.5-4.5-11.6-11.1-14.7-11.1-14.7-9.1-6.2.7-6.1.7-6.1 10.1.7 15.4 10.4 15.4 10.4 9 15.4 23.6 11 29.3 8.4.9-6.5 3.5-11 6.4-13.5-22.3-2.5-45.8-11.1-45.8-49.3 0-10.9 3.9-19.8 10.4-26.8-.9-2.5-4.5-12.7 1-26.5 0 0 8.5-2.7 27.5 10.2 8-2.2 16.6-3.3 25.1-3.3 8.5 0 17.1 1.1 25.1 3.3 19-12.9 27.5-10.2 27.5-10.2 5.5 13.8 2 24 1 26.5 6.5 7 10.4 15.9 10.4 26.8 0 38.3-23.5 46.8-45.9 49.3 3.6 3.1 6.8 9.2 6.8 18.6v27.5c0 2.7 1.8 5.8 6.8 4.8C171.3 181.7 200 144.2 200 100 200 44.8 155.2 0 100 0z"/></g>
                <path class="wire ai" d="M210 127l8-7 9 8 8-7M218 120v-7M227 128v9M235 121h8"/>
                <circle class="node" cx="218" cy="113" r="2.5"/><circle class="node" cx="227" cy="137" r="2.5"/><circle class="node" cx="243" cy="121" r="2.5"/>
            </g>
        </svg>
        <img class="hero-art hero-art-image" src="/static/hero-fusion.svg" alt="GitHub collaboration merging with AI automation" />
    </section>

    <div class="layout">
    <section class="panel">
      <div class="panel-head"><div class="panel-title"><span class="panel-icon">⌛</span><div><h2>Ticket queue</h2><p class="section-copy">Top 20 actionable tickets ranked by priority and urgency.</p></div></div><button class="subtle" onclick="loadTickets()">↻ &nbsp; Refresh</button></div>
      <div class="panel-body">
        <label class="field">Repository
          <input type="text" id="repository" value="pvfscaffolding/crm-staff-desktop" placeholder="owner/repository">
        </label>
        <p class="sync-meta">GitHub is the source of truth. Priority is read from P0/P1/P2 labels.</p>
        <div class="actions queue-actions"><button class="primary" onclick="syncGithub()">↻ &nbsp; Sync GitHub issues</button></div>
        <div id="ticketStatus"></div>
        <div class="table-wrap"><div id="ticketQueue"></div></div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><div class="panel-title"><span class="panel-icon">▣</span><div><h2>Selected ticket</h2><p class="section-copy">Choose a ticket from the queue or paste a GitHub issue URL.</p></div></div></div>
      <div class="panel-body">
        <div class="form-grid">
        <label class="field wide">Issue URL
          <input type="text" id="issueUrl" placeholder="https://github.com/owner/repo/issues/123">
        </label>
        <label class="field">Base branch
          <input type="text" id="baseBranch" value="develop">
        </label>
        <label class="field">Branch prefix
          <input type="text" id="branchPrefix" value="bug-fix">
        </label>
        </div>
        <div class="actions">
          <button class="primary" onclick="generateInvestigation()">⌁ &nbsp; Investigation prompt</button>
          <button class="subtle" onclick="generateReview()">◯ &nbsp; Review prompt</button>
        </div>
      </div>
    </section>
    </div>

    <div id="output" class="output"></div>
    <button id="copyBtn" class="subtle" onclick="copyOutput()" style="display:none;">Copy prompt</button>
</main>

    <script>
        async function generateInvestigation() {
            try {
                const issueUrl = document.getElementById('issueUrl').value.trim();
                const baseBranch = document.getElementById('baseBranch').value.trim();
                const branchPrefix = document.getElementById('branchPrefix').value.trim();

                if (!issueUrl) throw new Error('Issue URL required');

                const res = await fetch('/prompt/investigation', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({issue_url: issueUrl, base_branch: baseBranch, branch_prefix: branchPrefix})
                });
                const data = await res.json();
                showOutput(data.prompt || data.error);
            } catch (e) {
                showOutput(`Error: ${e.message}`);
            }
        }

        async function generateReview() {
            try {
                const issueUrl = document.getElementById('issueUrl').value.trim();
                const baseBranch = document.getElementById('baseBranch').value.trim();

                if (!issueUrl) throw new Error('Issue URL required');

                const res = await fetch('/prompt/review', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({issue_url: issueUrl, base_branch: baseBranch})
                });
                const data = await res.json();
                showOutput(data.prompt || data.error);
            } catch (e) {
                showOutput(`Error: ${e.message}`);
            }
        }

        function showOutput(text) {
            const output = document.getElementById('output');
            const copyBtn = document.getElementById('copyBtn');
            output.textContent = text;
            output.style.display = 'block';
            copyBtn.style.display = 'block';
            output.scrollIntoView({behavior: 'smooth'});
        }

        function copyOutput() {
            const output = document.getElementById('output');
            const text = output.textContent;
            navigator.clipboard.writeText(text).then(() => {
                const btn = document.getElementById('copyBtn');
                const original = btn.textContent;
                btn.textContent = 'Copied!';
                setTimeout(() => {
                    btn.textContent = original;
                }, 2000);
            }).catch(err => {
                alert('Failed to copy: ' + err);
            });
        }

        async function syncGithub() {
            const res = await fetch('/tickets/sync', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({repository:document.getElementById('repository').value})});
            const data = await res.json(); document.getElementById('ticketStatus').textContent = data.error || ('Synced ' + data.count + ' tickets'); loadTickets();
        }
        async function loadTickets() {
            const res = await fetch('/tickets'); const data = await res.json();
            document.getElementById('ticketQueue').innerHTML = '<table><tr><th>Rank</th><th>Ticket number</th><th>Priority</th><th>Status</th><th>Title</th><th>Assigned</th><th>Labels</th><th>Updated</th><th></th></tr>' + data.tickets.map((t, i) => {
                const priority = String(t.priority || '—');
                const status = String(t.project_status || 'Backlog');
                const assignee = String(t.assignees || 'Unassigned');
                const initial = assignee === 'Unassigned' ? '—' : assignee.charAt(0).toUpperCase();
                const statusClass = status.toLowerCase().includes('triage') ? ' status-triage' : '';
                return '<tr><td>'+(i+1)+'</td><td><a href="'+t.url+'" target="_blank">#'+t.number+'</a></td><td><span class="chip priority-'+priority.toLowerCase()+'">'+priority+'</span></td><td><span class="chip'+statusClass+'">'+status+'</span></td><td>'+t.title+'</td><td><span class="assignee"><span class="avatar">'+initial+'</span>'+assignee+'</span></td><td><span class="chip">'+(t.labels || '—')+'</span></td><td>'+String(t.updated_at||'').slice(0,10)+'</td><td><button class="use-ticket" data-ticket-url="'+encodeURIComponent(t.url)+'">Use ticket&nbsp; ›</button></td></tr>';
            }).join('') + '</table>';
            document.querySelectorAll('.use-ticket').forEach(button => button.addEventListener('click', () => useTicket(decodeURIComponent(button.dataset.ticketUrl))));
        }
        function useTicket(url) {
            document.getElementById('issueUrl').value = url;
            document.getElementById('issueUrl').scrollIntoView({behavior:'smooth', block:'center'});
            document.getElementById('issueUrl').focus();
            document.getElementById('ticketStatus').textContent = 'Selected ticket: ' + url;
        }
        loadTickets();
    </script>
</body>
</html>
"""


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
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_num}"],
            capture_output=True,
            text=True,
            timeout=10
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


app = Flask(__name__)
settings = Settings()
settings.ensure_directories()
store = JobStore(settings.database_path)


@app.get("/")
def root():
    return render_template_string(HTML)


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


@app.post('/tickets/sync')
def sync_tickets():
    try:
        repository = request.json.get('repository', '').strip()
        synced = sync_github(repository)
        store.prune_repository_tickets(repository, [ticket['key'] for ticket in synced])
        store.upsert_tickets(synced)
        return jsonify({'count': len(synced)})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


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


if __name__ == "__main__":
    port = find_available_port()
    print(f"Starting on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
