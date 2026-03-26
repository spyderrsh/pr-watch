# pr-watch

A local GitHub webhook listener that watches pull requests and triggers interactive AI coding CLI sessions in new terminal windows when PR events occur.

When a reviewer leaves comments, submits a review, or CI checks complete, pr-watch opens a new Windows Terminal window with Claude Code (or OpenCode) already loaded with the event context — so you can immediately analyze and respond.

## Features

- **Real-time webhook forwarding** via `gh webhook forward`
- **Event-specific slash commands** — prompts live as `.claude/commands/<event>.md` in your project
- **4-minute rolling debounce** — captures complete review bursts before firing
- **PR lifecycle awareness** — rejects watches that don't match the PR state, fires immediately for already-merged/closed PRs
- **Auto-cleanup** — watches auto-unregister on merge/close
- **Multi-watch safe** — generated commands are namespaced per repo/PR to avoid conflicts
- **Crash recovery** — restores watches on restart and catches up missed events via GitHub API
- **CLI-agnostic** — works with Claude Code and OpenCode

## Prerequisites

- Python 3.10+
- [GitHub CLI](https://cli.github.com/) (`gh`)
- `gh webhook` extension: `gh extension install cli/gh-webhook`
- [Windows Terminal](https://aka.ms/terminal) with a "Git Bash" profile

## Install

```bash
# Clone
git clone https://github.com/spyderrsh/pr-watch.git ~/tools/pr-watch

# Add to PATH
mkdir -p ~/.local/bin
cp ~/tools/pr-watch/pr-watch ~/.local/bin/pr-watch
chmod +x ~/.local/bin/pr-watch

# Verify
pr-watch --help
```

## Quick start

1. **Create slash command templates** in your project:

```bash
mkdir -p .claude/commands
```

`.claude/commands/review_comment.md`:
```markdown
Review comments have been posted on this PR. Please analyze each comment
from the webhook context below:

1. Read the comment text, file path, and line context
2. Categorize as: must-fix, suggestion, question, or nitpick
3. Present a summary table

Do NOT make code changes — present the analysis for the developer to decide.
```

`.claude/commands/merged.md`:
```markdown
This PR has been merged. Please:

1. Confirm the merge details from the webhook context below
2. List any follow-up tasks mentioned in the PR description
3. Summarize what was included in this merge
```

2. **Register a watch**:

```bash
pr-watch register \
  --repo owner/repo \
  --pr 123 \
  --cwd /c/Work/project \
  --on review_comment \
  --on merged
```

3. **Continue working** — when events fire, a new terminal opens with Claude analyzing them via your slash command.

## CLI Commands

| Command | Description |
|---------|-------------|
| `pr-watch start [--port N] [--foreground]` | Start the server (auto-starts on register) |
| `pr-watch register --repo R --pr N --on EVENT [--cli claude\|opencode]` | Register a PR watch |
| `pr-watch list` | List active watches |
| `pr-watch unregister --repo R --pr N` | Remove a watch |
| `pr-watch status` | Show server and forwarder health |
| `pr-watch stop` | Graceful shutdown |

## Event types

| Event | Triggers on | Debounced |
|-------|------------|-----------|
| `review_comment` | Review comments and threads | Yes (4 min) |
| `review` | Review submitted (approved/changes requested) | Yes (4 min) |
| `checks` | CI check completed | Yes (4 min) |
| `merged` | PR merged | No (immediate, auto-unregisters) |
| `closed` | PR closed without merge | No (immediate, auto-unregisters) |

## How it works

```
┌─────────────────────────────────────────────┐
│  pr-watch register --repo R --pr N --on E   │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  pr-watch server (localhost:8765)            │
│  - Manages gh webhook forward per repo      │
│  - Debounces events per PR per event type   │
│  - Writes generated slash commands          │
│  - Opens Windows Terminal with CLI session  │
└──────────────────┬──────────────────────────┘
                   ▼
   GitHub webhook → gh webhook forward → POST /webhook
                   ▼
   Event fires → reads .claude/commands/<event>.md
               → appends webhook JSON context
               → writes _pr-watch-<owner>-<repo>-<pr>-<event>.md
               → launches: wt -p "Git Bash" claude "/_pr-watch-..."
```

## State files

| File | Purpose |
|------|---------|
| `~/.claude/pr-watch-state.json` | Active watches, forwarder metadata |
| `~/.claude/pr-watch.log` | Server log |
| `~/.claude/pr-watch-server.pid` | PID file |
| `<cwd>/.claude/commands/_pr-watch-*` | Generated slash commands (auto-created) |

## License

MIT
