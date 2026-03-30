# pr-watch Specification

This document captures all requirements, design decisions, and architectural choices made during development. It serves as the authoritative reference for future AI sessions and contributors.

## Core Concept

A local webhook listener service that watches GitHub PRs and triggers interactive AI coding CLI sessions (Claude Code, OpenCode) in new terminal windows when PR events occur. The developer writes prompt templates as slash commands in their project, and pr-watch handles the webhook plumbing.

## Requirements

### Event Types

| Event Name | GitHub Webhook Events | Debounced | Auto-Unregisters |
|------------|----------------------|-----------|------------------|
| `review_comment` | `pull_request_review_comment`, `pull_request_review_thread` | Yes (4 min rolling) | No |
| `review` | `pull_request_review` (approved, changes_requested, commented) | Yes (4 min rolling) | No |
| `checks` | `check_run`, `check_suite` | Yes (4 min rolling) | No |
| `merged` | `pull_request` (action=closed, merged=true) | No (immediate) | Yes |
| `closed` | `pull_request` (action=closed, merged=false) | No (immediate) | Yes |

### Watch Lifecycle

- A `review`, `review_comment`, `checks`, or `review` (approval) event does **NOT** remove the watch. The watch persists until the PR is merged, closed, or manually unregistered.
- `merged` and `closed` events **fire their prompt first**, then auto-unregister the watch.
- Multiple events of the same type accumulate during the debounce window and are delivered together.
- Multiple PRs debounce independently and concurrently.

### PR State Validation on Registration

When registering a watch, the server checks the PR's current state via `gh api`:

- **PR is open**: All events are accepted for watching.
- **PR is already merged**:
  - `review_comment`, `review`, `checks` → **rejected** (no future events possible)
  - `merged` → **fires immediately** (opens terminal with merge context, does not create a persistent watch)
  - `closed` → **rejected** (PR was merged, not closed)
- **PR is already closed (not merged)**:
  - `review_comment`, `review`, `checks` → **rejected**
  - `closed` → **fires immediately**
  - `merged` → **rejected** (PR was closed without merge)

### Debouncing

- **Rolling 4-minute window** per PR per event type.
- Each new event resets the timer. This captures complete review bursts (reviewers often leave comments over 2-3 minutes).
- When the timer fires, all accumulated payloads are delivered together.
- `merged` and `closed` events bypass debounce entirely — they fire immediately.

### Prompt Templates (Slash Commands)

- Prompts live as **slash command files** in the project: `<cwd>/.claude/commands/<event>.md`
- The user writes the template. The server reads it, appends webhook JSON context, and writes a **generated command**: `<cwd>/.claude/commands/_pr-watch-<owner>-<repo>-<pr>-<event>.md`
- Generated commands are namespaced with owner, repo, and PR number to avoid conflicts when multiple watches exist on the same repo.
- The CLI is launched with the generated slash command: `claude "/_pr-watch-<owner>-<repo>-<pr>-<event>"`
- Raw webhook JSON payloads are appended to the prompt as a fenced code block under a `## Webhook Event Context` header.

### Terminal Invocation

- Each event batch opens a **new Windows Terminal window** using Git Bash profile.
- The session is **interactive** — the user can continue the conversation after the initial analysis.
- Command format: `wt.exe -p "Git Bash" --title "<title>" -d "<cwd>" claude --name "<title>" "/<slash-command>"`
- No `--session-id` or `--resume` — each invocation is self-contained. The slash command contains all necessary context.
- No `-p` (print mode) — sessions are always interactive.

### Webhook Forwarding

- Uses `gh webhook forward` to tunnel GitHub webhooks to localhost.
- One forwarder process per repo (shared across all PR watches for that repo).
- The forwarder receives ALL events for the repo; the server filters by PR number.
- Forwarder processes are managed by the server: started on first watch registration, killed when last watch for a repo is removed.
- A watchdog thread monitors forwarder health every 30 seconds. Dead forwarders are restarted with exponential backoff (30s → 60s → 120s → 5min max, up to 10 retries).
- Limitation: `gh webhook forward` allows only one user per repo simultaneously.

### Server

- **Zero third-party dependencies** — Python stdlib only (`http.server`, `threading`, `subprocess`, `json`).
- Uses `http.server.HTTPServer` with `ThreadingMixIn` for concurrent request handling.
- Default port: 8765.
- **Auto-starts** on first `pr-watch register` if not already running.
- State persisted to `~/.claude/pr-watch-state.json` via atomic write (`os.replace`).
- On restart: restores watches, restarts forwarders, runs catch-up via GitHub API for missed events.

### Catch-up on Restart

When the server starts with existing watches in state, it queries the GitHub API for events that occurred during downtime:
- `gh api repos/{repo}/pulls/{pr}/reviews` — for `review` events
- `gh api repos/{repo}/pulls/{pr}/comments` — for `review_comment` events
- Events newer than `last_event_at` are fed into the debounce system as if they arrived in real-time.

### CLI Interface

```
pr-watch start [--port N] [--foreground]
pr-watch register --repo OWNER/REPO --pr N --cwd PATH --on EVENT [--on EVENT ...] [--cli claude|opencode]
pr-watch list
pr-watch unregister --repo OWNER/REPO --pr N
pr-watch status
pr-watch stop
```

- `--on EVENT` takes only the event name (no file path). The command file is always at `<cwd>/.claude/commands/<EVENT>.md`.
- `--cli` defaults to `claude`. Also supports `opencode`.
- `register` validates command files exist before sending to server.

### Windows-Specific

- All `subprocess.run` and `subprocess.Popen` calls (except the terminal launch) use `CREATE_NO_WINDOW` to prevent console window flashes.
- The terminal launch uses `subprocess.Popen(cmd_str)` without `shell=True` or `CREATE_NO_WINDOW` — `wt.exe` handles its own window creation.
- Paths are normalized: forward slashes internally, Windows-style backslashes for `wt.exe -d` argument.
- Git Bash profile specified via `-p "Git Bash"` in the `wt.exe` command.

## File Layout

```
~/tools/pr-watch/
  server.py              # HTTP server
  pr_watch_cli.py        # CLI wrapper
  pr-watch               # Bash shim (symlinked to ~/.local/bin/)

~/.claude/
  pr-watch-state.json    # Runtime state
  pr-watch-server.pid    # PID file
  pr-watch.log           # Server log
  pr-watch-prompts/      # Temp files (legacy, may be removed)

~/.claude/skills/pr-watch/
  SKILL.md               # Agent skill for Claude Code

<project-cwd>/
  .claude/commands/
    <event>.md                                  # User's prompt template
    _pr-watch-<owner>-<repo>-<pr>-<event>.md   # Generated (template + context)
```

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/webhook` | Receive GitHub webhook events from `gh webhook forward` |
| `POST` | `/register` | Register a PR watch |
| `GET` | `/watches` | List all active watches |
| `DELETE` | `/watch?repo=X&pr=N` | Unregister a watch |
| `GET` | `/health` | Server status |
| `POST` | `/stop` | Graceful shutdown |

## Design Decisions Log

### Debounce: Rolling vs Fixed Window
**Decision**: Rolling (resets on each new event).
**Why**: Reviewers leave comments in bursts over 2-3 minutes. Rolling captures the complete burst before firing.

### Prompt Delivery: Slash Commands vs File Arguments
**Decision**: Slash commands in `.claude/commands/`.
**Why**: Keeps prompts in the project repo (versionable), avoids shell argument length limits with large webhook payloads, and integrates naturally with Claude Code's command system.

### Session Management: None (Self-Contained)
**Decision**: No `--session-id` or `--resume`. Each invocation is independent.
**Why**: Using `--session-id` assumes the user doesn't have another active session. The slash command contains all necessary context. Users can run `claude --continue` manually if they want to resume.

### Terminal: Interactive, Not `-p`
**Decision**: Always interactive mode.
**Why**: The user wants to interact with the analysis — discuss, ask follow-ups, or direct code changes. `-p` exits immediately.

### Generated Command Namespacing
**Decision**: `_pr-watch-<owner>-<repo>-<pr>-<event>.md`
**Why**: Multiple watches on the same repo would overwrite each other's generated commands without namespacing.

### Forwarder: One Per Repo
**Decision**: Single `gh webhook forward` per repo, server filters by PR.
**Why**: `gh webhook forward` doesn't support per-PR filtering. Multiple forwarders for the same repo cause "Hook already exists" errors.

### CLI-Agnostic Design
**Decision**: Support both Claude Code and OpenCode via `--cli` flag.
**Why**: The user wants the tool to work with multiple AI coding CLIs, not be locked to one.

### No Third-Party Dependencies
**Decision**: Python stdlib only.
**Why**: Instant deployment, no venv/pip step. The concurrency requirements are trivially low (<10 req/min).

### Subprocess Window Suppression
**Decision**: All background subprocess calls use `CREATE_NO_WINDOW`. Terminal launch does not.
**Why**: `gh api` and `gh webhook forward` flashed console windows without this flag. The terminal launch intentionally creates a visible window.
