# pr-watch Design & Implementation Notes

Implementation choices, workarounds, and architectural decisions made while building pr-watch. These are not requirements — they could be changed as long as the [SPEC](SPEC.md) is still satisfied.

---

## Architectural Decisions

### Debounce: Rolling vs Fixed Window
Rolling window (resets on each new event). Reviewers leave comments in bursts over 2-3 minutes; rolling captures the complete burst before firing.

### Prompt Delivery via Slash Commands
User writes `<cwd>/.claude/commands/<event>.md`. Server generates `_pr-watch-<owner>-<repo>-<pr>-<event>.md` with webhook context appended. Keeps prompts in the project repo (versionable), avoids shell argument length limits, and integrates with Claude Code's slash command system.

### Self-Contained Invocations (No Session Tracking)
No `--session-id` or `--resume`. Each invocation is independent. `--session-id` assumes the user doesn't have another active session open. The generated slash command contains all necessary context. Users can `claude --continue` manually if they want to resume.

### One Forwarder Per Repo
Single `gh webhook forward` per repo; the server filters by PR number. `gh webhook forward` doesn't support per-PR filtering, and multiple forwarders for the same repo cause "Hook already exists" errors.

### CLI Interface: Event Name Only
`--on EVENT` takes only the event name (no file path). The file is always at `<cwd>/.claude/commands/<EVENT>.md`. Reduces registration complexity.

### Zero Third-Party Dependencies
Python stdlib only (`http.server`, `threading`, `subprocess`, `json`). No venv/pip step needed. Concurrency load is trivially low (<10 req/min).

### Catch-up via GitHub API on Restart
On restart, query `gh api` for reviews/comments since `last_event_at` and feed them into the debounce system. Webhooks are lost when the server is down; the API provides recovery.

### Auto-Start on Register
`pr-watch register` starts the server if not already running. Eliminates a separate `pr-watch start` step.

---

## Implementation Details

### Threading Model
`http.server.HTTPServer` + `ThreadingMixIn` for request handling. `threading.Timer` for debounce timers. Simpler than asyncio with no external dependencies.

### State Persistence
JSON file at `~/.claude/pr-watch-state.json` with atomic writes via `os.replace`. Debounce timers are in-memory only and not restored on restart.

### Forwarder Health Watchdog
A daemon thread polls forwarder processes every 30 seconds. Dead forwarders restart with exponential backoff (30s, 60s, 120s, up to 5min max, 10 retries).

### Windows: Subprocess Window Suppression
All background subprocess calls (`gh api`, `gh webhook forward`) use `CREATE_NO_WINDOW` to prevent console window flashes. The terminal launch (`wt.exe`) does NOT use this flag since it intentionally creates a visible window.

### Windows: Terminal Launch
`wt.exe -p "Git Bash" --title "..." -d "..." claude --name "..." "/..."`. Uses `-p "Git Bash"` (profile, not window name). Command passed as a string to `subprocess.Popen` without `shell=True` to avoid `cmd.exe` ghost windows.

### Windows: Path Normalization
Forward slashes internally. Windows-style backslashes for `wt.exe -d` argument.

### PR State Check on Registration
Single `gh api repos/{owner}/{repo}/pulls/{pr}` call to get current state. Register endpoint timeout set to 30s to account for GitHub API latency.

### Generated Command File Location
Written to `<cwd>/.claude/commands/_pr-watch-<owner>-<repo>-<pr>-<event>.md`. The `_` prefix keeps them sorted separately from user commands. Owner/repo/PR namespacing prevents conflicts.

---

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
