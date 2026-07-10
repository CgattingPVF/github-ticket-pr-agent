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
- Codex CLI by default, already authenticated
- A clean, isolated machine or container is strongly recommended. The coding agent reads and executes repository code.

Check authentication:

```bash
gh auth status
codex login
```

The GitHub token used by `gh` needs enough repository access to read issues and code, push branches, create pull requests, and create issue/PR comments or reviews.

## Start on Windows PowerShell

```powershell
Copy-Item .env.example .env
.\run.ps1
```

Open `http://127.0.0.1:3060`.

## Run a ticket from the dashboard

Select a ticket and generate an investigation or review prompt.

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

The repository includes a GitHub Actions release workflow at `.github/workflows/release-windows.yml`.
Push a semantic version tag to build `ticket-pr-agent.exe` and attach it to a GitHub Release:

```bash
git add .github/workflows/release-windows.yml README.md
git commit -m "Add Windows release build"
git push origin main
git tag v1.0.0
git push origin v1.0.0
```

The executable still requires the GitHub CLI (`gh`) to be installed and authenticated on the Windows machine because the application uses it to read GitHub issues.

The application loads `.env` automatically from the project directory and also respects environment variables supplied by your shell, process manager, or container.

## Default Codex execution

The default command is:

```text
codex exec --sandbox workspace-write --ask-for-approval never -
```

The application passes the generated prompt over stdin and runs Codex with the cloned repository as its working directory. `workspace-write` allows edits only inside the workspace and avoids the deprecated `--full-auto` compatibility flag.

You can replace both commands in the web form or environment variables. Any replacement command must:

- Read the prompt from stdin.
- Run non-interactively.
- Make source edits in the current working directory for the coding pass.
- Create `.ticket-agent/result.json` for the coding pass.
- Create `.ticket-agent/review.json` for the review pass.

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
