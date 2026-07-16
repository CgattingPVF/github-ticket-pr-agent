from __future__ import annotations

import json


def investigation_prompt(
    issue: dict,
    base_branch: str,
    branch_name: str,
    repositories: list[str] | None = None,
    result_path: str = ".ticket-agent/result.json",
) -> str:
    labels = [label.get("name") for label in issue.get("labels", [])]
    issue_payload = {
        "url": issue.get("html_url"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "body": issue.get("body") or "",
        "labels": labels,
        "state": issue.get("state"),
    }
    return f"""
You are operating as a careful senior software engineer inside a checked-out application workspace.

TASK
Investigate GitHub ticket #{issue.get('number')} and implement the smallest safe fix.

TICKET
{json.dumps(issue_payload, indent=2)}

GIT CONTEXT
- Base branch: {base_branch}
- Working branch: {branch_name}
- Repositories in scope: {', '.join(repositories or ['current repository'])}

WORKSPACE RULES
- Inspect every repository in scope before deciding where the root cause belongs.
- You may change one or several repositories when the ticket requires a coordinated frontend/backend fix.
- Treat each listed repository as an independent Git repository. Do not edit other sibling projects.
- When the ticket requests persisted history and the current API lacks it, implement the smallest backward-compatible model, migration, create/list API, and client integration needed to satisfy the ticket.
- Follow established patterns in these repositories (especially the inspections history workflow) for naming, audit fields, authorization, migrations, API shape, and UI behavior.
- The ticket's expected behavior and suggested audit fields are sufficient product direction for a minimal implementation. Do not stop merely because a new persistence model or migration is required.

MANDATORY PROCESS
1. Read repository instructions first: AGENTS.md, CLAUDE.md, README, contributing guidance, package scripts, and nearby tests.
2. Investigate before editing. Trace the exact execution path and reproduce or prove the fault where practical.
3. Do not assume the ticket's proposed cause is correct. Establish the root cause using code, tests, logs, types, or data flow.
4. Make the smallest safe change. Required API schema changes and migrations are allowed when supported by repository conventions and tests. Do not make unrelated refactors, dependency upgrades, migrations, or formatting sweeps.
5. Preserve backwards compatibility unless the ticket explicitly requires a breaking change.
6. Do not add, update, rename, or delete test files. Pull requests created by this workflow must contain production changes only.
7. Run one focused pass/fail test for the changed behavior and the narrowest available application build check. Avoid exhaustive repository-wide suites unless the ticket specifically requires them.
8. Inspect the final diff for secrets, generated files, accidental deletions, debug code, and unrelated edits.
9. Do not commit, push, create a PR, modify GitHub, or change git remotes. The surrounding application handles GitHub operations.
10. Never claim certainty without evidence. If a concrete technical blocker remains after inspecting both repositories and their established patterns, make no speculative source edits and report it precisely.

REQUIRED OUTPUT
Create `{result_path}` with exactly this shape:
{{
  "safe_to_pr": true,
  "confidence": 0.0,
  "summary": "concise description of the implemented change",
  "root_cause": "specific, evidence-based root cause",
  "evidence": ["file:line or command/result"],
  "files_changed": ["relative/path"],
  "tests_run": [{{"command": "...", "result": "passed|failed|not-run", "notes": "..."}}],
  "unresolved_risks": [],
  "completion_requirements": ["ticket-specific action still required before this can be completed; empty when none"],
  "commit_message": "imperative commit message",
  "pr_title": "concise PR title",
  "pr_notes": "anything reviewers must know"
}}

Set `safe_to_pr` to false only for a concrete merge-blocking problem: validation failure, uncertain root cause, unavailable required access, an unsafe/destructive migration, or an ambiguity that cannot be resolved from the ticket and established repository patterns. Required schema/API/database work is not itself a blocker. When blocked, make `completion_requirements` a precise, ticket-specific checklist (for example: create the missing table and migration, define the absent API contract, obtain access to a named service, or fix a named failing check). Do not put generic advice such as "retry the job" there. When unblocked, use an empty list. Put ordinary deployment sequencing and reviewer guidance in `pr_notes`, not `unresolved_risks`. Confidence must be between 0 and 1.
""".strip()


def all_in_one_prompt(issue: dict, base_branch: str, branch_name: str) -> str:
    """Build a single prompt whose agent pauses for approval before each stage."""
    labels = [label.get("name") for label in issue.get("labels", [])]
    issue_payload = {
        "url": issue.get("html_url"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "body": issue.get("body") or "",
        "labels": labels,
        "state": issue.get("state"),
    }
    return f"""
You are a careful senior software engineer working in a checked-out repository.
Complete the GitHub ticket below as one guided workflow, but obtain permission before every stage.

TICKET
{json.dumps(issue_payload, indent=2)}

GIT CONTEXT
- Base branch: {base_branch}
- Working branch: {branch_name}

INTERACTIVE STAGE CONTROL (MANDATORY)
- Before starting each stage, explain what you found, what you intend to do, and the files or external effects involved.
- Then ask exactly: "Proceed with stage <number> (<name>)? (yes/no)"
- Wait for the user's answer. Start the stage only after an explicit "yes" (case-insensitive).
- If the answer is "no", stop immediately, make no further changes, and report the stopped stage.
- Never combine confirmations, assume consent from an earlier answer, or continue after a timeout/ambiguous answer.
- Ask for confirmation before any source edit, validation command, review, repair, commit, push, PR creation, or GitHub comment/review.

STAGES
1. Investigate the ticket and repository instructions; reproduce or prove the root cause.
2. Implement the smallest safe fix without changing test files. Do not commit, push, or access GitHub.
3. Inspect the diff and run the relevant tests, lint, type checks, or builds.
4. Independently review the change without editing source files.
5. If the review has HIGH or CRITICAL findings, ask permission before repairing them, then repeat validation and review as needed.
6. Ask permission before committing and pushing the branch.
7. Ask permission before creating the pull request and posting the review/link on GitHub.

Do not make unrelated refactors, weaken tests, change remotes, or claim certainty without evidence. Stop and explain any blocker.
At the end, create `.ticket-agent/result.json` using the standard ticket-agent result schema, including evidence, files changed, tests, risks, commit message, and PR title.
""".strip()


def review_prompt(issue: dict, base_branch: str) -> str:
    return f"""
Act as an independent senior code reviewer. Review the current working-tree changes against `origin/{base_branch}` for GitHub ticket #{issue.get('number')}: {issue.get('title')}.

RULES
- Review only the changed diff and the directly affected surrounding code. Do not scan the entire repository or run a full test suite.
- Finish within a few minutes; produce the review artifact as soon as the changed paths have been assessed.
- The checkout is an isolated read-only review snapshot. Do not edit, delete, rename, format, or create source files.
- Your only permitted write is `.ticket-agent/review.json`. Report suggested fixes as findings; never implement them during review.
- Check correctness, regression risk, security, authorization, data integrity, concurrency, error handling, compatibility, tests, and whether the change actually addresses the ticket's root cause.
- Ignore cosmetic preferences unless they create a real maintenance or correctness problem.
- Findings must be specific and actionable. Use HIGH only for a merge-blocking defect, security issue, data-loss risk, or clear failure to satisfy the ticket.
- Line numbers must refer to lines on the RIGHT side of a changed file when possible.
- Do not approve merely because tests pass.

Create `.ticket-agent/review.json` with this shape:
{{
  "verdict": "PASS|COMMENT|BLOCK",
  "summary": "review summary",
  "findings": [
    {{
      "severity": "CRITICAL|HIGH|MEDIUM|LOW|INFO",
      "title": "short title",
      "body": "specific explanation and required change",
      "path": "relative/file/path",
      "line": 123,
      "side": "RIGHT"
    }}
  ]
}}

Use PASS only when no material findings remain. Use BLOCK when any HIGH or CRITICAL finding exists.
""".strip()


def repair_prompt(issue: dict, review: dict, result_path: str = ".ticket-agent/result.json") -> str:
    return f"""
The current change for GitHub ticket #{issue.get('number')} received the automated review below:

{json.dumps(review, indent=2)}

Re-investigate each HIGH or CRITICAL finding. Fix it only if the finding is valid. Keep the change minimal, rerun relevant checks, and update `{result_path}` with fresh evidence, test results, confidence, and risks. Do not commit, push, or access GitHub. Do not silence tests or weaken validation merely to make checks pass.
""".strip()


def test_plan_prompt(issue: dict, result_path: str) -> str:
    labels = [label.get("name") for label in issue.get("labels", [])]
    issue_payload = {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "body": issue.get("body") or "",
        "labels": labels,
    }
    return f"""
Read the GitHub ticket below. Do not inspect or edit any repository; this is a documentation-only task.

TICKET
{json.dumps(issue_payload, indent=2)}

Write `{result_path}` with exactly this shape:
{{
  "repro_steps": ["numbered, concrete step to reproduce the reported problem"],
  "pass_steps": ["numbered, concrete step to verify the fix satisfies the ticket"]
}}

Base every step only on what the ticket states or clearly implies. Keep each list short (3-6 items), specific, and actionable by a human tester who has not read the code.
""".strip()


def confidence_gate_prompt(issue: dict, error: str, result_path: str = ".ticket-agent/result.json") -> str:
    return f"""
The automated workflow for GitHub ticket #{issue.get('number')} rejected your last `{result_path}` for this reason:

{error}

The gate requires `safe_to_pr: true`, `confidence >= 0.90`, and an EMPTY `unresolved_risks` list — a non-empty list blocks the gate regardless of severity.

For each item currently in `unresolved_risks`:
- If it is a real defect, fix it and rerun the relevant checks.
- If it is genuinely out of scope or acceptable to ship as-is, do NOT leave it in `unresolved_risks`. Move it into `pr_notes` (or `completion_requirements` if the ticket cannot be completed at all) with a short justification, and remove it from `unresolved_risks`.

Then rewrite `{result_path}` in full with every required key: safe_to_pr, confidence, summary, root_cause, evidence, files_changed, tests_run, unresolved_risks, completion_requirements, commit_message, pr_title, pr_notes. Do not commit, push, or access GitHub.
""".strip()
