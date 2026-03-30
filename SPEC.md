# pr-watch Specification

## Core Concept

A local service that watches GitHub PRs for events (reviews, comments, CI checks, merges, closures) and triggers interactive AI coding CLI sessions in new terminal windows. The developer writes prompt templates in their project, and pr-watch handles the webhook plumbing.

---

## Requirements

### Watching & Events

- The service watches a specific PR on a specific repo for one or more event types.
- Supported event types: `review_comment`, `review`, `checks`, `merged`, `closed`.
- Each event type has its own prompt — registered separately per event.
- A watch can be registered for any combination of events on a single PR.

### Watch Lifecycle

- A `review`, `review_comment`, `checks`, or approval event does **NOT** remove the watch. The watch persists until the PR is merged, closed, or manually unregistered.
- `merged` and `closed` events **fire their prompt first**, then auto-unregister the watch.
- If a watch is added to a PR that is already merged or closed:
  - Events that require an open PR (`review_comment`, `review`, `checks`) are **rejected**.
  - `merged` fires immediately if the PR is already merged. `closed` fires immediately if the PR is already closed.
  - Mismatched terminal events are rejected (e.g., `merged` on a closed-without-merge PR).

### Debouncing

- Events are debounced with a 4-minute rolling window, per PR, per event type.
- Multiple PRs debounce independently and concurrently.
- `merged` and `closed` events bypass debounce — they fire immediately.

### Prompt Delivery

- Prompts live as slash command files in the project: `<cwd>/.claude/commands/<event>.md`.
- The prompt file name is never assumed — it always matches the event name.
- Each watch's generated command must be unique (no conflicts when multiple watches exist on the same repo or across repos).
- Raw webhook JSON payloads are appended to the prompt as context.

### Terminal & Interaction

- Each event batch opens a **new terminal window** with an **interactive** AI session (not non-interactive/print mode).
- The user can interact with the AI after the initial prompt is processed.
- No ghost windows or console flashes when events fire.

### CLI Tool Agnostic

- Must work with both Claude Code and OpenCode (independent of any specific CLI).
- The CLI tool is specified at registration time.

### Webhook Transport

- Uses `gh webhook forward` to tunnel GitHub webhooks to localhost.
- The service manages forwarder processes automatically.

### Server Behavior

- The server auto-starts on first `pr-watch register` if not already running.
- Can also be started/stopped manually.
- State persists across restarts.
- On restart, the server should check for events that occurred while it was down and process them.

### Interface

- Script + CLI wrapper + agent skill that explains to an agentic LLM how to use the CLI.
