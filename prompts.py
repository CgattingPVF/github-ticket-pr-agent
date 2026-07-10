from __future__ import annotations

import json


def investigation_prompt(issue: dict, base_branch: str, branch_name: str) -> str:
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
You are operating as a careful senior software engineer inside a checked-out Git repository.

TASK
Investigate GitHub ticket #{issue.get('number')} and implement the smallest safe fix.

TICKET
{json.dumps(issue_payload, indent=2)}

GIT CONTEXT
- Base branch: {base_branch}
- Working branch: {branch_name}

MANDATORY PROCESS
1. Read repository instructions first: AGENTS.md, CLAUDE.md, README, contributing guidance, package scripts, and nearby tests.
2. Investigate before editing. Trace the exact execution path and reproduce or prove the fault where practical.
3. Do not assume the ticket's proposed cause is correct. Establish the root cause using code, tests, logs, types, or data flow.
4. Make the smallest safe change. Do not make unrelated refactors, dependency upgrades, schema changes, migrations, or formatting sweeps.
5. Preserve backwards compatibility unless the ticket explicitly requires a breaking change.
6. Add or update focused tests that fail before the fix and pass after it where practical.
7. Run the most relevant tests, type checks, lint checks, or build checks available locally.
8. Inspect the final diff for secrets, generated files, accidental deletions, debug code, and unrelated edits.
9. Do not commit, push, create a PR, modify GitHub, or change git remotes. The surrounding application handles GitHub operations.
10. Never claim certainty without evidence. If the issue cannot be safely fixed, make no speculative source edits and report the blocker.

REQUIRED OUTPUT
Create `.ticket-agent/result.json` with exactly this shape:
{{
  "safe_to_pr": true,
  "confidence": 0.0,
  "summary": "concise description of the implemented change",
  "root_cause": "specific, evidence-based root cause",
  "evidence": ["file:line or command/result"],
  "files_changed": ["relative/path"],
  "tests_run": [{{"command": "...", "result": "passed|failed|not-run", "notes": "..."}}],
  "unresolved_risks": [],
  "commit_message": "imperative commit message",
  "pr_title": "concise PR title",
  "pr_notes": "anything reviewers must know"
}}

Set `safe_to_pr` to false if validation fails, the root cause remains uncertain, required access is unavailable, or the change needs product/schema approval. Confidence must be between 0 and 1.
""".strip()


def review_prompt(issue: dict, base_branch: str) -> str:
    return f"""
Act as an independent senior code reviewer. Review the current working-tree changes against `origin/{base_branch}` for GitHub ticket #{issue.get('number')}: {issue.get('title')}.

RULES
- Review the diff and relevant surrounding code. Do not edit source files.
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


def repair_prompt(issue: dict, review: dict) -> str:
    return f"""
The current change for GitHub ticket #{issue.get('number')} received the automated review below:

{json.dumps(review, indent=2)}

Re-investigate each HIGH or CRITICAL finding. Fix it only if the finding is valid. Keep the change minimal, rerun relevant checks, and update `.ticket-agent/result.json` with fresh evidence, test results, confidence, and risks. Do not commit, push, or access GitHub. Do not silence tests or weaken validation merely to make checks pass.
""".strip()
