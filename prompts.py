from __future__ import annotations

import json


def _truncate_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n...[truncated {len(text) - limit} chars]"


def _compact_json(value: object) -> str:
    """Render machine-readable prompt context without pretty-print token overhead."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _without_empty(value: dict) -> dict:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _bounded_issue_json(payload: dict, limit: int) -> str:
    """Keep ticket JSON valid while fitting the stage's context budget."""
    bounded = json.loads(_compact_json(payload))
    if len(_compact_json(bounded)) <= limit:
        return _compact_json(bounded)
    bounded["body_excerpt"] = _truncate_text(bounded.get("body_excerpt"), 700)
    bounded["latest_comments"] = (bounded.get("latest_comments") or [])[-1:]
    for comment in bounded.get("latest_comments", []):
        comment["body_excerpt"] = _truncate_text(comment.get("body_excerpt"), 160)
    bounded["linked_pull_requests"] = (bounded.get("linked_pull_requests") or [])[:1]
    for pull_request in bounded.get("linked_pull_requests", []):
        pull_request["checks"] = (pull_request.get("checks") or [])[:3]
        for key in ("url", "base", "head"):
            pull_request.pop(key, None)
    rendered = _compact_json(_without_empty(bounded))
    if len(rendered) > limit:
        excess = len(rendered) - limit
        bounded["body_excerpt"] = _truncate_text(
            bounded.get("body_excerpt"), max(240, len(str(bounded.get("body_excerpt", ""))) - excess - 40),
        )
        rendered = _compact_json(_without_empty(bounded))
    if len(rendered) > limit:
        bounded.pop("linked_pull_requests", None)
        rendered = _compact_json(_without_empty(bounded))
    if len(rendered) > limit:
        bounded.pop("latest_comments", None)
        rendered = _compact_json(_without_empty(bounded))
    return rendered


def compact_issue_payload(
    issue: dict,
    *,
    body_limit: int = 1500,
    comment_limit: int = 5,
    pull_request_limit: int = 5,
) -> dict:
    """Return the bounded GitHub context used in agent prompts."""
    labels = [
        _truncate_text(label.get("name"), 80)
        for label in issue.get("labels", [])[:8]
        if isinstance(label, dict) and label.get("name")
    ]
    context = issue.get("mergequest_github_context") or {}
    payload = _without_empty({
        "url": _truncate_text(issue.get("html_url"), 300),
        "number": issue.get("number"),
        "title": _truncate_text(issue.get("title"), 240),
        "body_excerpt": _truncate_text(issue.get("body"), body_limit),
        "labels": labels,
        "state": issue.get("state"),
    })
    if context:
        comments = (
            (context.get("latest_comments") or [])[-comment_limit:]
            if comment_limit > 0 else []
        )
        payload["latest_comments"] = [
            _without_empty({
                "author": _truncate_text(item.get("author"), 80),
                "created_at": item.get("created_at"),
                "body_excerpt": _truncate_text(item.get("body"), 260),
            })
            for item in comments
            if isinstance(item, dict)
        ]
        payload["linked_pull_requests"] = [
            _without_empty({
                "repository": _truncate_text(item.get("repository"), 150),
                "number": item.get("number"),
                "title": _truncate_text(item.get("title"), 180),
                "state": item.get("state"),
                "url": _truncate_text(item.get("url"), 300),
                "base": _truncate_text(item.get("base"), 100),
                "head": _truncate_text(item.get("head"), 100),
                "changed_files": item.get("changed_files"),
                "checks": [
                    _without_empty({
                        "name": _truncate_text(check.get("name"), 100),
                        "status": _truncate_text(check.get("status"), 40),
                    })
                    for check in (item.get("checks") or [])[:6]
                    if isinstance(check, dict)
                ],
            })
            for item in (context.get("linked_pull_requests") or [])[:max(0, pull_request_limit)]
            if isinstance(item, dict)
        ]
        if context.get("context_warnings"):
            payload["context_warnings"] = [
                _truncate_text(item, 180) for item in context["context_warnings"][:3]
            ]
        if not payload.get("latest_comments"):
            payload.pop("latest_comments", None)
        if not payload.get("linked_pull_requests"):
            payload.pop("linked_pull_requests", None)
    return payload


def investigation_prompt(
    issue: dict,
    base_branch: str,
    branch_name: str,
    repositories: list[str] | None = None,
    result_path: str = ".ticket-agent/result.json",
    run_validation: bool = False,
) -> str:
    issue_payload = compact_issue_payload(issue, body_limit=1200, comment_limit=3, pull_request_limit=2)
    validation_rule = (
        "Run only one focused behavior test and the narrowest relevant build check; no broad suite."
        if run_validation
        else "DO NOT run tests, builds, type checks, lint, code/Prisma generation, Cargo checks, or the application. "
        "The surrounding Integrity Scan handles validation. Record tests_run as []."
    )
    return f"""
FULL DELIVERY SPEED CONTRACT — HIGHEST PRIORITY
- Target a safe implementation within 90 seconds. {validation_rule}
- Start with `git status --short` and `git diff` in each listed repository; resume existing ticket changes.
- Use targeted `rg` and bounded file reads. Never scan parent directories or use broad `find ..` searches.
- Read only applicable AGENTS.md/CLAUDE.md; skip general docs/manifests unless one fact is needed. Batch reads; stop once root cause is proven.
- Write `{result_path}` immediately after the production diff.

TASK: prove ticket #{issue.get('number')}'s root cause and implement the smallest safe production fix.
CONTEXT: {_bounded_issue_json(issue_payload, 1250)}
GIT: base={base_branch}; branch={branch_name}; repos={_compact_json(repositories or ['current repository'])}

MANDATORY
1. Inspect every scoped repo, but no sibling projects. Follow its established patterns and preserve compatibility.
2. Verify the reported cause with code/data flow before editing. No speculative edits, unrelated refactors, dependencies, formatting sweeps, or test-file changes.
3. Required backward-compatible persistence/schema/API/client work is allowed and is not a blocker.
4. For each changed field, trace applicable input/UI -> client mapping -> create/update API -> persistence/migration -> read query -> hydration/edit-save -> requested export/report. Add evidence per boundary.
5. Inspect the final diff for secrets, generated/debug/unrelated files and accidental deletion. Run `git status --short` in every changed repo; every `files_changed` path must remain modified/untracked. Never clean/reset/restore/reverse the implementation.
6. Do not commit, push, create a PR, access GitHub, or change remotes. Claim confidence only from evidence.

REQUIRED OUTPUT
Create `{result_path}` as exactly this JSON object:
{{"safe_to_pr":true,"confidence":0.0,"summary":"change","root_cause":"evidence-based cause","evidence":["file:line or command/result"],"files_changed":["relative/path"],"tests_run":[{{"command":"...","result":"passed|failed|not-run","notes":"..."}}],"unresolved_risks":[],"completion_requirements":[],"commit_message":"imperative message","pr_title":"title","pr_notes":"review/deployment notes"}}
Confidence is 0..1. `safe_to_pr=false` only for a concrete merge blocker (failed required validation, unproven cause, missing required access, unsafe migration, or irresolvable ambiguity). When blocked, put precise ticket-specific actions—not "retry"—in `completion_requirements`. When ready, both risk/requirement lists are empty; put non-blocking guidance in `pr_notes`.
""".strip()


def all_in_one_prompt(issue: dict, base_branch: str, branch_name: str) -> str:
    """Build a single prompt whose agent pauses for approval before each stage."""
    issue_payload = compact_issue_payload(issue, body_limit=1600, comment_limit=3, pull_request_limit=2)
    return f"""
You are a careful senior software engineer working in a checked-out repository.
Complete the GitHub ticket below as one guided workflow, but obtain permission before every stage.

TICKET
{_bounded_issue_json(issue_payload, 1800)}

GIT CONTEXT
- Base branch: {base_branch}
- Working branch: {branch_name}

INTERACTIVE STAGE CONTROL (MANDATORY)
- Before each stage, briefly state findings, intent, files, and external effects; then ask exactly: "Proceed with stage <number> (<name>)? (yes/no)"
- Start only after an explicit case-insensitive yes. A no stops all work. Never combine confirmations, reuse earlier consent, or continue after timeout/ambiguity.
- Ask for confirmation before any source edit, validation command, review, repair, commit, push, PR creation, or GitHub comment/review.

STAGES
1. Investigate the ticket and repository instructions; reproduce or prove the root cause.
2. Implement the smallest safe fix without changing test files. Do not commit, push, or access GitHub.
3. Inspect the diff and run the relevant tests, lint, type checks, or builds.
4. Independently review the change without editing source files.
5. If the review has HIGH or CRITICAL findings, ask permission before repairing them, then repeat validation and review as needed.
6. Ask permission before committing and pushing the branch.
7. Ask permission before creating the pull request and posting the review/link on GitHub.

No unrelated refactors, weakened tests, remote changes, or unsupported certainty. Stop on a blocker. Finally create `.ticket-agent/result.json` using the standard schema with evidence, files, tests, risks, commit message, and PR title.
""".strip()


def _compact_implementation_context(implementation: dict | None) -> dict:
    source = implementation or {}
    coordinated: dict[str, dict] = {}
    for repository, detail in list((source.get("coordinated_repository_changes") or {}).items())[:3]:
        if not isinstance(detail, dict):
            continue
        coordinated[_truncate_text(repository, 150)] = _without_empty({
            "working_tree": [_truncate_text(line, 160) for line in (detail.get("working_tree") or [])[:20]],
            "diff": _truncate_text(detail.get("diff"), 800),
            "diff_truncated": detail.get("diff_truncated"),
        })
    return _without_empty({
        "summary": _truncate_text(source.get("summary"), 240),
        "root_cause": _truncate_text(source.get("root_cause"), 350),
        "evidence": [_truncate_text(item, 160) for item in (source.get("evidence") or [])[:5]],
        "files_changed": [_truncate_text(item, 180) for item in (source.get("files_changed") or [])[:20]],
        "validation": [
            _without_empty({
                "command": _truncate_text(item.get("command"), 140),
                "result": item.get("result"),
                "notes": _truncate_text(item.get("notes"), 160),
            })
            for item in (source.get("tests_run") or [])[:4]
            if isinstance(item, dict)
        ],
        "coordinated_repository_changes": coordinated,
    })


def review_prompt(
    issue: dict,
    base_branch: str,
    implementation: dict | None = None,
    output_path: str = ".ticket-agent/review.json",
) -> str:
    ticket_context = compact_issue_payload(
        issue, body_limit=1200, comment_limit=2, pull_request_limit=2,
    )
    implementation_context = _compact_implementation_context(implementation)
    return f"""
Act as an independent senior code reviewer. Review the current working-tree changes against `origin/{base_branch}`.

TICKET REQUIREMENTS
{_bounded_issue_json(ticket_context, 1100)}

IMPLEMENTATION CLAIMS (verify these; do not trust them blindly)
{_compact_json(implementation_context)}

RULES
- The process working directory is the isolated checkout of the target repository. Run every repository command there. Never `cd` to the ticket-agent/controller repository or inspect its Git history.
- Review only changed diff and directly affected definitions; no repository scan/full suite. Start with `git status --short`, `git diff --stat`, and `git diff`. If `origin/{base_branch}` is unavailable, review the supplied working-tree diff against `HEAD`; do not leave the checkout to search another repository.
- This can create coordinated pull requests in multiple repositories. Treat supplied `coordinated_repository_changes` as one plan. Do not report a required sibling change as missing when its manifest/diff includes it; report only absent/incompatible changes or unsafe ordering.
- Read-only snapshot: do not edit/delete/rename/format/create source. The only write is the review result at `{output_path}`; never implement findings.
- Check ticket fit, root cause, correctness, regression, security/auth, data integrity, concurrency, errors, compatibility, and tests. Do not approve merely because tests pass.
- For each changed field verify create/update -> persistence -> reads -> client hydration/edit-save -> outputs, across coordinated repos and generated clients when applicable.
- Findings must be concrete/actionable. HIGH means merge blocker, security, data loss, or clear ticket failure. Ignore harmless cosmetics. Use RIGHT-side changed line numbers when possible.

Write `{output_path}` exactly as {{"verdict":"PASS|COMMENT|BLOCK","summary":"review summary","findings":[{{"severity":"CRITICAL|HIGH|MEDIUM|LOW|INFO","title":"short title","body":"specific explanation and required change","path":"relative/file/path","line":123,"side":"RIGHT"}}]}}. PASS only with no material finding; BLOCK for any HIGH/CRITICAL.
""".strip()


def repair_prompt(
    issue: dict,
    review: dict,
    result_path: str = ".ticket-agent/result.json",
    run_validation: bool = False,
) -> str:
    validation_instruction = (
        "Rerun the relevant checks and report their results."
        if run_validation else
        "Do not run tests, builds, type checks, lint, or the application; the surrounding Integrity Scan handles validation."
    )
    findings = [
        _without_empty({
            "severity": item.get("severity"),
            "title": _truncate_text(item.get("title"), 180),
            "body": _truncate_text(item.get("body"), 420),
            "path": _truncate_text(item.get("path"), 220),
            "line": item.get("line"),
        })
        for item in (review.get("findings") or [])
        if isinstance(item, dict)
        and str(item.get("severity", "")).upper() in {"HIGH", "CRITICAL", "BLOCKER"}
    ][:6]
    review_context = _without_empty({
        "verdict": review.get("verdict"),
        "summary": _truncate_text(review.get("summary"), 500),
        "blocking_findings": findings,
    })
    return f"""
Ticket #{issue.get('number')} review: {_compact_json(review_context)}
Re-investigate every blocking finding; fix only valid ones and keep changes minimal. {validation_instruction} Rewrite `{result_path}` with fresh evidence, confidence, and risks. Do not commit/push/access GitHub or weaken validation.
""".strip()


def test_plan_prompt(issue: dict, result_path: str) -> str:
    labels = [label.get("name") for label in issue.get("labels", [])]
    issue_payload = {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "body_excerpt": _truncate_text(issue.get("body"), 1200),
        "labels": labels,
    }
    return f"""
Read the GitHub ticket below. Do not inspect or edit any repository; this is a documentation-only task.

TICKET
{_compact_json(_without_empty(issue_payload))}

Write `{result_path}` exactly as {{"repro_steps":["concrete step"],"pass_steps":["concrete step"]}}. Use only stated/clearly implied behavior; each list must have 3-6 short, specific steps for a tester unfamiliar with the code.
""".strip()


def automated_qa_prompt(issue: dict, result_path: str) -> str:
    """Build a read-only, evidence-first QA run for a ticket."""
    issue_payload = compact_issue_payload(
        issue, body_limit=1400, comment_limit=0, pull_request_limit=0,
    )
    return f"""
You are the independent QA agent for this GitHub ticket:
{_compact_json(issue_payload)}

TESTING ONLY in CRM_APP_PVF. Read applicable repo instructions and inspect relevant crm-staff-desktop/crm-api changes. Never edit production, commit/push/branch/PR/access GitHub, or leave files changed. A temporary proof file must be deleted.

- Derive 3-8 focused checks from acceptance criteria: success, one boundary/negative, and adjacent regression. Inspect the merge-base diff first and ensure checks reach changed code. Run the smallest relevant existing unit/integration/API/type/lint command; static inspection alone is insufficient and must name file/symbol/behavior.
- No full app, packaging, Tauri, generation, migration, formatter, production build, browser driver, Playwright/Cypress/Selenium/Puppeteer, screenshots, or UI launch. Check `git status --short` before/after. If a command changes tracked files, stop and mark it failed; never hide/restore it.
- Missing behavior is failed/incomplete, never passed. Run commands and report real exits; never invent a pass.

Repository-wide checks need careful attribution. For a non-zero command, inspect diagnostics: mark failed only when caused by, reaching, or preventing the ticket change. If all diagnostics are in an unrelated pre-existing file, mark skipped with exact exit/path evidence, note baseline noise, and run a narrower check. Never claim a non-zero command passed or suppress/edit errors.

Manual UI testing is outside this automated report. Do not add UI interaction, appearance, browser/window behavior, missing Windows session, or missing running app as passed/failed/skipped entries; mention UI-only criteria in the summary for human verification. Skipped is only for relevant non-UI automation infrastructure.

Write `{result_path}` exactly as {{"summary":"evidence-based conclusion","overall":"passed|failed|incomplete","tests_run":[{{"command":"executed command/proof","result":"passed|failed|skipped","notes":"observed evidence"}}]}}. Include at least one executed result.
""".strip()


def confidence_gate_prompt(
    issue: dict,
    error: str,
    result_path: str = ".ticket-agent/result.json",
    run_validation: bool = False,
    minimum_confidence: float = 0.80,
) -> str:
    defect_instruction = (
        "fix it and rerun the relevant checks."
        if run_validation else
        "fix it, but do not run tests, builds, type checks, lint, or the application; the surrounding Integrity Scan handles validation."
    )
    return f"""
Ticket #{issue.get('number')} gate rejected `{result_path}`: {_truncate_text(error, 1200)}

Required: `safe_to_pr: true`, `confidence >= {minimum_confidence:.2f}`, and EMPTY `unresolved_risks`. For each risk: if a real defect, {defect_instruction} Otherwise move justified non-blocking guidance to `pr_notes`, or a true blocker action to `completion_requirements`; remove it from risks.

Rewrite `{result_path}` in full with: safe_to_pr, confidence, summary, root_cause, evidence, files_changed, tests_run, unresolved_risks, completion_requirements, commit_message, pr_title, pr_notes. Do not commit/push/access GitHub.
""".strip()
