# Ticket PR Agent

A local Flask application that accepts a GitHub issue URL and a selected base branch, then:

1. Fetches the original ticket.
2. Clones the repository through the authenticated GitHub CLI.
3. Creates a new working branch from the selected base branch.
4. Gives a coding agent a strict investigation-and-fix prompt.
5. Enforces confidence, unresolved-risk, diff, and validation gates.
6. Runs a separate automated code-review pass.
7. Gives the coding agent one configurable repair cycle for blocking findings.
8. Commits and pushes only after all gates pass.
9. Creates a pull request, posts a formal review, and comments the PR URL on the original ticket.
10. Never merges automatically.

No program can honestly guarantee a fix is “100% correct.” This application treats certainty as an evidence gate rather than decorative optimism: it stops if the agent reports uncertainty, tests fail, unresolved risks remain, or the review finds a blocker.

## Requirements

- Python 3.10+
- `git`
- GitHub CLI `gh`, already authenticated with access to the target repositories
- Codex CLI by default, already authenticated (Claude Code CLI also works, see below)
- A clean, isolated machine or container is strongly recommended. The coding agent reads and executes repository code.

Check authentication:

```bash
gh auth status
codex login
```

The GitHub token used by `gh` needs enough repository access to read issues and code, push branches, create pull requests, and create issue/PR comments or reviews.

Enable **Comment on the ticket automatically when the job fails** when starting a job to post the failed stage, blocker, and next steps on the original issue. Set `COMMENT_ON_FAILURE=true` to enable this option by default. Failure reporting is best-effort and never replaces the job's original error.

## Start on Windows PowerShell

```powershell
Copy-Item .env.example .env
.\run.ps1
```

Open `http://127.0.0.1:3060`.

## Run a ticket from the dashboard

Select a ticket and generate an investigation, review, or all-in-one prompt. The all-in-one prompt runs the complete workflow conversationally and requires an explicit yes/no confirmation before each stage, including edits, validation, GitHub actions, and PR creation.

Codex is used for ticket execution:

```text
AGENT_COMMAND=codex exec --sandbox workspace-write --ask-for-approval never -
```

The editor opener is configurable with `EDITOR_COMMAND`; the default is `code --reuse-window`. If VS Code is not available on the worker machine, the guarded workflow continues and records that it could not open the editor. The web UI does not attempt fragile prompt injection into an IDE panel; the prompt is delivered directly to the CLI process over stdin.

## Start on macOS/Linux

```bash
cp .env.example .env
./run.sh
```

## Build a Windows executable

On a Windows machine with Python 3.10 or newer installed, run:

```powershell
.\build-windows.ps1
```

The script creates an isolated build environment, installs the application and
PyInstaller dependencies, runs the tests, and writes the executable to
`dist\ticket-pr-agent.exe`. To build without rerunning the tests, use
`.\build-windows.ps1 -SkipTests`.

PyInstaller builds for the operating system it runs on, so the Windows `.exe`
must be produced on Windows (or by the Windows GitHub Actions runner below).

The repository includes a GitHub Actions workflow at `.github/workflows/release-windows.yml`.

When a GitHub Release is published, the workflow:

1. Checks out the commit referenced by the release tag.
2. Runs the Python test suite.
3. Builds `ticket-pr-agent.exe` with PyInstaller on a Windows runner.
4. Attaches the executable to the published release under **Assets**.

To publish a build:

1. Open **Releases** in GitHub.
2. Select **Draft a new release**.
3. Create or select a version tag such as `v1.0.0`.
4. Publish the release.
5. Wait for the **Build Windows release** workflow to complete, then download `ticket-pr-agent.exe` from the release assets.

The workflow can also be run manually from the **Actions** tab. Manual runs upload the executable as a temporary workflow artifact but do not modify a GitHub Release.

The executable bundles the official GitHub CLI (`gh`) used to read and update
GitHub issues. The build downloads the latest Windows CLI archive and verifies
it against GitHub's published SHA-256 checksum before packaging it. GitHub
authentication is still user-specific: authenticate an installed GitHub CLI
once with `gh auth login`, or provide `GH_TOKEN` when starting the application.

The application loads `.env` automatically from the project directory and also respects environment variables supplied by your shell, process manager, or container.

## Default Codex execution

The default command is:

```text
codex exec --sandbox workspace-write --ask-for-approval never -
```

The application passes the generated prompt over stdin. For a local `CRM_APP_PVF` workspace, Codex runs from the workspace root and can coordinate changes across `crm-staff-desktop` and `crm-api`; each changed repository is validated, reviewed, committed, pushed, and submitted as a separate linked PR. Other tickets retain the single-repository workflow. `workspace-write` allows edits only inside the selected workspace and avoids the deprecated `--full-auto` compatibility flag.

You can replace both commands in the web form or environment variables. Any replacement command must:

- Read the prompt from stdin.
- Run non-interactively.
- Make source edits in the current working directory for the coding pass.
- Create `.ticket-agent/result.json` for the coding pass.
- Create `.ticket-agent/review.json` for the review pass.

## Using Claude Code instead of (or alongside) Codex

The agent and review commands are independent, so each can point at a different CLI. To use Claude Code for the coding pass:

```text
AGENT_COMMAND=claude -p --output-format text --dangerously-skip-permissions
```

To mix both — e.g. Codex writes the fix, Claude reviews it — set `AGENT_COMMAND` to the Codex command and `REVIEW_COMMAND` to the Claude command (or the reverse), either via `.env` or per-job in the web form. Run `claude login` (or `claude setup-token`) once to authenticate, same as `codex login`.

The Advanced loadout now exposes Codex, Claude Code, and Custom command independently for coding and review. Claude uses `CLAUDE_COMMAND` and receives the same stdin prompts and `.ticket-agent` JSON contract, so investigation, confidence gates, validation, review, repair cycles, and PR creation are available with Claude as either pass or both passes.

## Validation commands

Add one command per line in the form, for example:

```text
bun test
bun run lint
bun run typecheck
```

or:

```text
python -m pytest
python -m ruff check .
```

Commands are parsed as argument lists and are **not** executed through a shell. This deliberately prevents `&&`, pipes, redirects, and command substitution from turning a web field into a general shell console. Every job also runs `git diff --check`.

## GitHub linking behaviour

The PR body references the original ticket, and the application posts the PR URL directly on that ticket. If **Close issue on merge** is enabled and the selected base branch is the repository's default branch, the PR body uses a supported closing keyword. For a non-default base such as `develop`, it uses `Relates to` so the ticket is linked without pretending the intermediate PR will close it.

## Safety boundaries

- Binds to localhost by default. Do not expose this development app directly to a network.
- Does not accept arbitrary shell syntax in validation commands.
- Does not merge PRs.
- Does not push when confidence is below the configured threshold.
- Does not push when the agent reports unresolved risks or failed tests.
- Does not push while HIGH/CRITICAL review findings remain.
- Stores each checkout in a separate job workspace.
- Excludes `.ticket-agent/` metadata from commits.

For team deployment, put the worker in an ephemeral container/VM, add real authentication and CSRF protection to the UI, use a GitHub App instead of a user login, restrict allowed organisations/repositories/base branches, and queue jobs through a proper worker service rather than an in-process thread. The local MVP is intentionally not masquerading as production infrastructure. Humanity has suffered enough from that particular tradition.

## Run tests

```bash
python -m pytest
```

## Files

- `app.py`: Flask routes and UI entry point
- `workflow.py`: guarded end-to-end orchestration
- `github_ops.py`: `gh` and `git` operations
- `prompts.py`: investigation, review, and repair prompts
- `core.py`: parsing, command execution, gates, and formatting
- `store.py`: SQLite job state and logs
