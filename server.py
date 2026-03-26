#!/usr/bin/env python3
"""pr-watch server: Local GitHub webhook listener that triggers AI coding CLIs."""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8765
DEBOUNCE_SECONDS = 240  # 4 minutes
WATCHDOG_INTERVAL = 30  # seconds
FORWARDER_BACKOFF_BASE = 30  # seconds
FORWARDER_BACKOFF_MAX = 300  # 5 minutes
FORWARDER_MAX_RETRIES = 10

STATE_DIR = Path.home() / ".claude"
STATE_FILE = STATE_DIR / "pr-watch-state.json"
PID_FILE = STATE_DIR / "pr-watch-server.pid"
LOG_FILE = STATE_DIR / "pr-watch.log"
PROMPT_DIR = STATE_DIR / "pr-watch-prompts"

WT_PATH = Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "wt.exe"


# Map user-friendly event names to GitHub webhook event types
USER_EVENT_TO_GITHUB = {
    "review_comment": ["pull_request_review_comment", "pull_request_review_thread"],
    "review": ["pull_request_review"],
    "checks": ["check_run", "check_suite"],
    "merged": ["pull_request"],
    "closed": ["pull_request"],
}

# Reverse: GitHub event type -> list of user event names it can match
GITHUB_TO_USER_EVENTS = {}
for user_evt, gh_evts in USER_EVENT_TO_GITHUB.items():
    for gh_evt in gh_evts:
        GITHUB_TO_USER_EVENTS.setdefault(gh_evt, []).append(user_evt)

# All possible GitHub events we might need
ALL_GITHUB_EVENTS = sorted(
    {evt for evts in USER_EVENT_TO_GITHUB.values() for evt in evts}
)

# Subprocess kwargs to suppress console window flashes on Windows
_NO_WINDOW = {}
if sys.platform == "win32":
    _NO_WINDOW["creationflags"] = subprocess.CREATE_NO_WINDOW

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("pr-watch")


def setup_logging(foreground: bool = False):
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if foreground:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class WatchRegistration:
    repo: str
    pr: int
    cwd: str
    cli: str  # "claude" | "opencode"
    events: list  # list of user event names (command files at <cwd>/.claude/commands/<event>.md)
    sessions: dict = field(default_factory=dict)  # user_event_name -> session_id | None
    last_event_at: str | None = None  # ISO timestamp
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.pr}"

    def command_file(self, event: str) -> Path:
        """Path to the user's slash command template for this event."""
        return Path(self.cwd) / ".claude" / "commands" / f"{event}.md"

    def generated_command_file(self, event: str) -> Path:
        """Path to the generated slash command (template + webhook context).
        Includes repo and PR number to avoid conflicts across watches."""
        safe_repo = self.repo.replace("/", "-")
        return Path(self.cwd) / ".claude" / "commands" / f"_pr-watch-{safe_repo}-{self.pr}-{event}.md"

    def generated_slash_command(self, event: str) -> str:
        """Slash command name for the generated command file."""
        safe_repo = self.repo.replace("/", "-")
        return f"/_pr-watch-{safe_repo}-{self.pr}-{event}"

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "pr": self.pr,
            "cwd": self.cwd,
            "cli": self.cli,
            "events": self.events,
            "sessions": self.sessions,
            "last_event_at": self.last_event_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatchRegistration":
        events = d["events"]
        # Migration: handle old dict format -> list
        if isinstance(events, dict):
            events = list(events.keys())
        return cls(
            repo=d["repo"],
            pr=d["pr"],
            cwd=d["cwd"],
            cli=d.get("cli", "claude"),
            events=events,
            sessions=d.get("sessions", {}),
            last_event_at=d.get("last_event_at"),
            created_at=d.get("created_at", ""),
        )


@dataclass
class DebounceEntry:
    watch_key: str
    user_event: str
    timer: threading.Timer
    payloads: list = field(default_factory=list)
    deadline: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ForwarderInfo:
    repo: str
    process: subprocess.Popen | None = None
    events: list = field(default_factory=list)
    started_at: str = ""
    retry_count: int = 0
    last_failure: str | None = None


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

class PRWatchState:
    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.lock = threading.RLock()
        self.watches: dict[str, WatchRegistration] = {}
        self.forwarders: dict[str, ForwarderInfo] = {}
        self.debounces: dict[str, DebounceEntry] = {}
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._shutdown_event = threading.Event()

    # -- Persistence --

    def load(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for key, wd in data.get("watches", {}).items():
                self.watches[key] = WatchRegistration.from_dict(wd)
            logger.info("Loaded %d watches from state file", len(self.watches))
        except Exception as e:
            logger.error("Failed to load state: %s", e)

    def persist(self):
        try:
            data = {
                "server": {
                    "port": self.port,
                    "started_at": self.started_at,
                },
                "watches": {k: w.to_dict() for k, w in self.watches.items()},
                "forwarders": {
                    repo: {
                        "events": fi.events,
                        "started_at": fi.started_at,
                        "retry_count": fi.retry_count,
                    }
                    for repo, fi in self.forwarders.items()
                },
            }
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(STATE_FILE))
        except Exception as e:
            logger.error("Failed to persist state: %s", e)

    # -- Watch management --

    def register_watch(self, watch: WatchRegistration) -> dict:
        with self.lock:
            existing = self.watches.get(watch.key)
            if existing:
                # Merge events into existing watch
                for evt in watch.events:
                    if evt not in existing.events:
                        existing.events.append(evt)
                    if evt not in existing.sessions:
                        existing.sessions[evt] = None
                existing.cwd = watch.cwd
                existing.cli = watch.cli
            else:
                for evt in watch.events:
                    watch.sessions.setdefault(evt, None)
                self.watches[watch.key] = watch

            self._ensure_forwarder(watch.repo)
            self.persist()

            return {
                "status": "registered",
                "key": watch.key,
                "events": list((existing or watch).events.keys()),
            }

    def unregister_watch(self, repo: str, pr: int) -> dict:
        key = f"{repo}#{pr}"
        with self.lock:
            if key not in self.watches:
                return {"status": "not_found", "key": key}

            # Cancel any pending debounces for this watch
            to_cancel = [dk for dk in self.debounces if dk.startswith(f"{key}::")]
            for dk in to_cancel:
                entry = self.debounces.pop(dk)
                entry.timer.cancel()

            del self.watches[key]

            # Check if any other watches use this repo
            repo_still_needed = any(w.repo == repo for w in self.watches.values())
            if not repo_still_needed:
                self._stop_forwarder(repo)

            self.persist()
            return {"status": "unregistered", "key": key}

    def list_watches(self) -> list[dict]:
        with self.lock:
            result = []
            for key, w in self.watches.items():
                pending = {}
                for dk, de in self.debounces.items():
                    if dk.startswith(f"{key}::"):
                        evt = dk.split("::", 1)[1]
                        pending[evt] = {
                            "payload_count": len(de.payloads),
                            "fires_at": de.deadline.isoformat(),
                        }
                result.append({
                    **w.to_dict(),
                    "key": key,
                    "pending_debounce": pending,
                })
            return result

    # -- Forwarder management --

    def _compute_needed_github_events(self, repo: str) -> list[str]:
        events = set()
        for w in self.watches.values():
            if w.repo == repo:
                for user_evt in w.events:
                    for gh_evt in USER_EVENT_TO_GITHUB.get(user_evt, []):
                        events.add(gh_evt)
        return sorted(events)

    def _ensure_forwarder(self, repo: str):
        needed = self._compute_needed_github_events(repo)
        if not needed:
            return

        existing = self.forwarders.get(repo)
        if existing and existing.process and existing.process.poll() is None:
            # Running — check if events need updating
            if set(existing.events) == set(needed):
                return
            # Need to restart with new event set
            logger.info("Restarting forwarder for %s (events changed)", repo)
            self._stop_forwarder(repo)

        self._start_forwarder(repo, needed)

    def _start_forwarder(self, repo: str, events: list[str]):
        # Clean up any dead forwarder entry first
        existing = self.forwarders.get(repo)
        if existing and existing.process:
            if existing.process.poll() is not None:
                # Dead — read stderr for diagnostics
                try:
                    stderr = existing.process.stderr.read().decode("utf-8", errors="replace") if existing.process.stderr else ""
                    if stderr:
                        logger.warning("Previous forwarder stderr for %s: %s", repo, stderr.strip())
                except Exception:
                    pass
                del self.forwarders[repo]
            else:
                # Still alive — don't start a duplicate
                logger.info("Forwarder for %s already running (PID %d), skipping", repo, existing.process.pid)
                return

        event_csv = ",".join(events)
        cmd = [
            "gh", "webhook", "forward",
            "--repo", repo,
            "--events", event_csv,
            "--url", f"http://localhost:{self.port}/webhook",
        ]
        logger.info("Starting forwarder: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_NO_WINDOW,
            )
            self.forwarders[repo] = ForwarderInfo(
                repo=repo,
                process=proc,
                events=events,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.info("Forwarder started for %s (PID %d)", repo, proc.pid)
        except Exception as e:
            logger.error("Failed to start forwarder for %s: %s", repo, e)

    def _stop_forwarder(self, repo: str):
        fi = self.forwarders.pop(repo, None)
        if fi and fi.process:
            try:
                fi.process.terminate()
                fi.process.wait(timeout=5)
                logger.info("Stopped forwarder for %s", repo)
            except Exception as e:
                logger.warning("Error stopping forwarder for %s: %s", repo, e)
                try:
                    fi.process.kill()
                except Exception:
                    pass

    def restart_all_forwarders(self):
        """Restart forwarders for all repos with active watches (used on startup)."""
        repos = {w.repo for w in self.watches.values()}
        for repo in repos:
            needed = self._compute_needed_github_events(repo)
            if needed:
                self._start_forwarder(repo, needed)

    # -- Debounce system --

    def feed_event(self, watch_key: str, user_event: str, payload: dict):
        debounce_key = f"{watch_key}::{user_event}"

        with self.lock:
            # Update last_event_at
            watch = self.watches.get(watch_key)
            if watch:
                watch.last_event_at = datetime.now(timezone.utc).isoformat()

            # Check if merged/closed — bypass debounce
            if user_event in ("merged", "closed"):
                self.persist()
                self._fire_event(watch_key, user_event, [payload])
                # Auto-unregister
                if watch:
                    logger.info("PR %s %s — auto-unregistering watch", watch_key, user_event)
                    repo, pr = watch.repo, watch.pr
                    self.unregister_watch(repo, pr)
                return

            if debounce_key in self.debounces:
                entry = self.debounces[debounce_key]
                entry.payloads.append(payload)
                entry.timer.cancel()
                entry.timer = threading.Timer(
                    DEBOUNCE_SECONDS, self._fire_debounce, args=[debounce_key]
                )
                entry.timer.daemon = True
                entry.timer.start()
                entry.deadline = datetime.now(timezone.utc) + timedelta(seconds=DEBOUNCE_SECONDS)
                logger.info(
                    "Debounce reset for %s (%d payloads, fires at %s)",
                    debounce_key, len(entry.payloads), entry.deadline.isoformat(),
                )
            else:
                timer = threading.Timer(
                    DEBOUNCE_SECONDS, self._fire_debounce, args=[debounce_key]
                )
                timer.daemon = True
                timer.start()
                entry = DebounceEntry(
                    watch_key=watch_key,
                    user_event=user_event,
                    timer=timer,
                    payloads=[payload],
                    deadline=datetime.now(timezone.utc) + timedelta(seconds=DEBOUNCE_SECONDS),
                )
                self.debounces[debounce_key] = entry
                logger.info(
                    "Debounce started for %s (fires at %s)",
                    debounce_key, entry.deadline.isoformat(),
                )

            self.persist()

    def _fire_debounce(self, debounce_key: str):
        with self.lock:
            entry = self.debounces.pop(debounce_key, None)
            if not entry:
                return
            payloads = entry.payloads
            watch_key = entry.watch_key
            user_event = entry.user_event

        logger.info("Debounce fired for %s (%d payloads)", debounce_key, len(payloads))
        self._fire_event(watch_key, user_event, payloads)

    def _fire_event(self, watch_key: str, user_event: str, payloads: list[dict],
                    watch_override: WatchRegistration | None = None):
        with self.lock:
            watch = watch_override or self.watches.get(watch_key)
            if not watch:
                logger.warning("Watch %s not found when firing event", watch_key)
                return
            if user_event not in watch.events:
                logger.warning("Event %s not in watch %s", user_event, watch_key)
                return
            session_id = watch.sessions.get(user_event)
            cli = watch.cli
            cwd = watch.cwd
            pr = watch.pr
            repo = watch.repo

        # Read the user's slash command template
        template_file = watch.command_file(user_event)
        try:
            template_content = template_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Cannot read command file %s: %s", template_file, e)
            return

        # Build generated slash command: template + webhook context
        payload_json = json.dumps(payloads, indent=2)
        combined = (
            f"{template_content}\n\n"
            f"---\n\n"
            f"## Webhook Event Context\n\n"
            f"**Event type:** `{user_event}`\n"
            f"**PR:** #{pr} on {repo}\n"
            f"**Payloads received:** {len(payloads)}\n\n"
            f"```json\n{payload_json}\n```\n"
        )

        # Write generated command file into <cwd>/.claude/commands/_pr-watch-<event>.md
        generated_file = watch.generated_command_file(user_event)
        generated_file.parent.mkdir(parents=True, exist_ok=True)
        generated_file.write_text(combined, encoding="utf-8")
        slash_command = watch.generated_slash_command(user_event)
        logger.info("Wrote generated command: %s", generated_file)

        # Launch interactive CLI session with the slash command
        self._launch_terminal(cli, cwd, pr, user_event, slash_command)

    def _launch_terminal(
        self, cli: str, cwd: str, pr: int, user_event: str,
        slash_command: str,
    ):
        title = f"PR-{pr}-{user_event}"
        # Use Windows-style path for wt -d (wt is a Windows app)
        cwd_win = cwd.replace("/", "\\")
        wt = str(WT_PATH)

        if cli == "claude":
            cli_part = f'claude --name "{title}" "{slash_command}"'
        elif cli == "opencode":
            cli_part = f'opencode --title "{title}" "{slash_command}"'
        else:
            logger.error("Unknown CLI: %s", cli)
            return

        # Use -p "Git Bash" for the profile (shell type), not -w (window name)
        # No shell=True to avoid cmd.exe ghost window
        cmd_str = f'{wt} -p "Git Bash" --title "{title}" -d "{cwd_win}" {cli_part}'

        logger.info("Launching terminal: %s", cmd_str)
        try:
            subprocess.Popen(cmd_str)
        except Exception as e:
            logger.error("Failed to launch terminal: %s", e)

    # -- Catch-up system --

    def run_catchup(self):
        """Check GitHub API for events missed during downtime."""
        with self.lock:
            watches = list(self.watches.values())

        for watch in watches:
            if not watch.last_event_at:
                continue

            logger.info("Running catch-up for %s (since %s)", watch.key, watch.last_event_at)
            try:
                self._catchup_reviews(watch)
                self._catchup_comments(watch)
            except Exception as e:
                logger.error("Catch-up failed for %s: %s", watch.key, e)

    def _catchup_reviews(self, watch: WatchRegistration):
        if "review" not in watch.events:
            return

        cmd = ["gh", "api", f"repos/{watch.repo}/pulls/{watch.pr}/reviews", "--paginate"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **_NO_WINDOW)
            if result.returncode != 0:
                logger.warning("gh api failed for reviews: %s", result.stderr)
                return

            reviews = json.loads(result.stdout)
            if not isinstance(reviews, list):
                return

            cutoff = watch.last_event_at
            for review in reviews:
                submitted = review.get("submitted_at", "")
                if submitted > cutoff:
                    # Synthesize a payload similar to webhook format
                    payload = {
                        "action": "submitted",
                        "review": review,
                        "pull_request": {"number": watch.pr},
                        "repository": {"full_name": watch.repo},
                        "_catchup": True,
                    }
                    self.feed_event(watch.key, "review", payload)
        except Exception as e:
            logger.error("Catch-up reviews error for %s: %s", watch.key, e)

    def _catchup_comments(self, watch: WatchRegistration):
        if "review_comment" not in watch.events:
            return

        cmd = ["gh", "api", f"repos/{watch.repo}/pulls/{watch.pr}/comments", "--paginate"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **_NO_WINDOW)
            if result.returncode != 0:
                logger.warning("gh api failed for comments: %s", result.stderr)
                return

            comments = json.loads(result.stdout)
            if not isinstance(comments, list):
                return

            cutoff = watch.last_event_at
            for comment in comments:
                created = comment.get("created_at", "")
                updated = comment.get("updated_at", created)
                if max(created, updated) > cutoff:
                    payload = {
                        "action": "created",
                        "comment": comment,
                        "pull_request": {"number": watch.pr},
                        "repository": {"full_name": watch.repo},
                        "_catchup": True,
                    }
                    self.feed_event(watch.key, "review_comment", payload)
        except Exception as e:
            logger.error("Catch-up comments error for %s: %s", watch.key, e)

    # -- Watchdog --

    def watchdog_loop(self):
        """Monitor forwarder processes, restart if dead."""
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(WATCHDOG_INTERVAL)
            if self._shutdown_event.is_set():
                break

            with self.lock:
                for repo, fi in list(self.forwarders.items()):
                    if fi.process and fi.process.poll() is not None:
                        exit_code = fi.process.returncode
                        # Capture stderr for diagnostics
                        stderr_msg = ""
                        try:
                            stderr_msg = fi.process.stderr.read().decode("utf-8", errors="replace").strip() if fi.process.stderr else ""
                        except Exception:
                            pass
                        logger.warning(
                            "Forwarder for %s died (exit %d, retries: %d)%s",
                            repo, exit_code, fi.retry_count,
                            f"\n  stderr: {stderr_msg}" if stderr_msg else "",
                        )
                        fi.retry_count += 1
                        fi.last_failure = datetime.now(timezone.utc).isoformat()

                        if fi.retry_count > FORWARDER_MAX_RETRIES:
                            logger.error(
                                "Forwarder for %s exceeded max retries (%d), giving up",
                                repo, FORWARDER_MAX_RETRIES,
                            )
                            continue

                        # Exponential backoff
                        delay = min(
                            FORWARDER_BACKOFF_BASE * (2 ** (fi.retry_count - 1)),
                            FORWARDER_BACKOFF_MAX,
                        )
                        logger.info("Restarting forwarder for %s in %ds", repo, delay)
                        # Schedule restart
                        t = threading.Timer(delay, self._restart_forwarder, args=[repo])
                        t.daemon = True
                        t.start()

    def _restart_forwarder(self, repo: str):
        with self.lock:
            if repo not in self.forwarders:
                return
            needed = self._compute_needed_github_events(repo)
            if needed:
                self._start_forwarder(repo, needed)

    # -- Shutdown --

    def shutdown(self):
        logger.info("Shutting down...")
        self._shutdown_event.set()

        with self.lock:
            # Cancel all debounce timers
            for dk, entry in self.debounces.items():
                entry.timer.cancel()
            self.debounces.clear()

            # Stop all forwarders
            for repo in list(self.forwarders.keys()):
                self._stop_forwarder(repo)

            self.persist()

        # Remove PID file
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    server: "ThreadedHTTPServer"

    @property
    def state(self) -> PRWatchState:
        return self.server.state

    def log_message(self, format, *args):
        logger.debug("HTTP: %s", format % args)

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    # -- Routing --

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._handle_health()
        elif parsed.path == "/watches":
            self._handle_list_watches()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/webhook":
            self._handle_webhook()
        elif parsed.path == "/register":
            self._handle_register()
        elif parsed.path == "/stop":
            self._handle_stop()
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/watch":
            self._handle_unregister(parsed)
        else:
            self._send_json(404, {"error": "not found"})

    # -- Endpoint handlers --

    def _handle_health(self):
        with self.state.lock:
            forwarder_status = {}
            for repo, fi in self.state.forwarders.items():
                alive = fi.process and fi.process.poll() is None
                forwarder_status[repo] = {
                    "alive": alive,
                    "events": fi.events,
                    "started_at": fi.started_at,
                    "retry_count": fi.retry_count,
                    "last_failure": fi.last_failure,
                }

        self._send_json(200, {
            "status": "ok",
            "port": self.state.port,
            "started_at": self.state.started_at,
            "watch_count": len(self.state.watches),
            "forwarders": forwarder_status,
        })

    def _handle_list_watches(self):
        watches = self.state.list_watches()
        self._send_json(200, {"watches": watches})

    def _handle_register(self):
        try:
            body = self._read_body()
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        repo = body.get("repo")
        pr = body.get("pr")
        cwd = body.get("cwd")
        cli = body.get("cli", "claude")
        events = body.get("events", [])

        # Validation
        errors = []
        if not repo or "/" not in repo:
            errors.append("repo must be in 'owner/repo' format")
        if not isinstance(pr, int) or pr <= 0:
            errors.append("pr must be a positive integer")
        if not cwd:
            errors.append("cwd is required")
        if not events or not isinstance(events, list):
            errors.append("'events' must be a non-empty list of event names")
        if cli not in ("claude", "opencode"):
            errors.append("cli must be 'claude' or 'opencode'")

        # Validate each event name and check for its command file in cwd
        cwd_path = Path(cwd)
        for evt_name in events:
            if evt_name not in USER_EVENT_TO_GITHUB:
                errors.append(
                    f"unknown event '{evt_name}', must be one of: "
                    f"{', '.join(USER_EVENT_TO_GITHUB.keys())}"
                )
            else:
                cmd_file = cwd_path / ".claude" / "commands" / f"{evt_name}.md"
                if not cmd_file.is_file():
                    errors.append(
                        f"command file not found: {cmd_file}\n"
                        f"  Create it at <cwd>/.claude/commands/{evt_name}.md"
                    )

        if errors:
            self._send_json(400, {"errors": errors})
            return

        # Check PR state on GitHub — reject events that don't make sense
        pr_state = self._check_pr_state(repo, pr)
        if pr_state.get("error"):
            self._send_json(400, {"errors": [pr_state["error"]]})
            return

        is_open = pr_state.get("state") == "open"
        is_merged = pr_state.get("merged", False)
        is_closed = not is_open and not is_merged

        # Events that require an open PR
        OPEN_ONLY_EVENTS = {"review_comment", "review", "checks"}
        immediate_fire = []
        rejected = []
        watch_events = []

        for evt_name in events:
            if evt_name in OPEN_ONLY_EVENTS and not is_open:
                state_desc = "merged" if is_merged else "closed"
                rejected.append(f"'{evt_name}' rejected: PR is already {state_desc}")
            elif evt_name == "merged":
                if is_merged:
                    immediate_fire.append(evt_name)
                elif is_closed:
                    rejected.append("'merged' rejected: PR was closed without merge")
                else:
                    watch_events.append(evt_name)
            elif evt_name == "closed":
                if is_closed:
                    immediate_fire.append(evt_name)
                elif is_merged:
                    rejected.append("'closed' rejected: PR was merged, not closed")
                else:
                    watch_events.append(evt_name)
            else:
                watch_events.append(evt_name)

        # Fire immediate events (merged/closed on already-terminal PRs)
        if immediate_fire:
            for evt_name in immediate_fire:
                temp_watch = WatchRegistration(
                    repo=repo, pr=pr, cwd=cwd, cli=cli,
                    events=[evt_name],
                )
                self.state._fire_event(temp_watch.key, evt_name, [pr_state.get("raw", {})],
                                       watch_override=temp_watch)
                logger.info("Immediate fire for %s#%d::%s (PR already %s)",
                            repo, pr, evt_name, "merged" if is_merged else "closed")

        # Register remaining watch events (if any)
        result = {"status": "registered", "key": f"{repo}#{pr}", "events": []}
        if watch_events:
            watch = WatchRegistration(
                repo=repo, pr=pr, cwd=cwd, cli=cli, events=watch_events,
            )
            result = self.state.register_watch(watch)

        if rejected:
            result["rejected"] = rejected
        if immediate_fire:
            result["fired_immediately"] = immediate_fire
        if not watch_events and not immediate_fire:
            result["status"] = "no_events"
            result["message"] = "All events were rejected for the current PR state"

        status_code = 200 if (watch_events or immediate_fire) else 400
        self._send_json(status_code, result)

    def _check_pr_state(self, repo: str, pr: int) -> dict:
        """Query GitHub API for PR state (single call)."""
        cmd = ["gh", "api", f"repos/{repo}/pulls/{pr}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, **_NO_WINDOW)
            if result.returncode != 0:
                return {"error": f"Failed to check PR state: {result.stderr.strip()}"}
            raw = json.loads(result.stdout)
            return {
                "state": raw.get("state"),
                "merged": raw.get("merged", False),
                "title": raw.get("title"),
                "raw": {"pull_request": raw, "repository": {"full_name": repo}},
            }
        except Exception as e:
            return {"error": f"Failed to check PR state: {e}"}

    def _handle_unregister(self, parsed):
        qs = parse_qs(parsed.query)
        repo = qs.get("repo", [None])[0]
        pr_str = qs.get("pr", [None])[0]

        if not repo or not pr_str:
            self._send_json(400, {"error": "repo and pr query params required"})
            return

        try:
            pr = int(pr_str)
        except ValueError:
            self._send_json(400, {"error": "pr must be an integer"})
            return

        result = self.state.unregister_watch(repo, pr)
        status = 200 if result["status"] != "not_found" else 404
        self._send_json(status, result)

    def _handle_webhook(self):
        event_type = self.headers.get("X-GitHub-Event", "")
        delivery_id = self.headers.get("X-GitHub-Delivery", "unknown")

        try:
            body = self._read_body()
        except Exception as e:
            logger.warning("Bad webhook payload (delivery %s): %s", delivery_id, e)
            self._send_json(400, {"error": str(e)})
            return

        # Always return 200 quickly
        self._send_json(200, {"status": "received"})

        # Process in background
        t = threading.Thread(
            target=self._process_webhook,
            args=(event_type, body, delivery_id),
            daemon=True,
        )
        t.start()

    def _process_webhook(self, event_type: str, body: dict, delivery_id: str):
        logger.debug("Webhook: event=%s delivery=%s", event_type, delivery_id)

        # Extract PR number and repo
        pr_number = None
        repo = None

        if event_type in ("pull_request_review", "pull_request_review_comment", "pull_request_review_thread"):
            pr_data = body.get("pull_request", {})
            pr_number = pr_data.get("number")
            repo = body.get("repository", {}).get("full_name")

        elif event_type == "pull_request":
            pr_number = body.get("number")
            repo = body.get("repository", {}).get("full_name")

        elif event_type in ("check_run", "check_suite"):
            check_data = body.get(event_type, {})
            prs = check_data.get("pull_requests", [])
            if prs:
                pr_number = prs[0].get("number")
            repo = body.get("repository", {}).get("full_name")

        if not pr_number or not repo:
            logger.debug("Webhook ignored: no PR# found (event=%s)", event_type)
            return

        watch_key = f"{repo}#{pr_number}"

        # Find matching watch
        with self.state.lock:
            watch = self.state.watches.get(watch_key)
            if not watch:
                logger.debug("No watch for %s", watch_key)
                return

        # Map GitHub event to user event(s)
        user_events = GITHUB_TO_USER_EVENTS.get(event_type, [])

        for user_event in user_events:
            # Special handling for pull_request events
            if event_type == "pull_request":
                action = body.get("action", "")
                merged = body.get("pull_request", {}).get("merged", False)

                if user_event == "merged" and action == "closed" and merged:
                    if "merged" in watch.events:
                        logger.info("PR %s merged!", watch_key)
                        self.state.feed_event(watch_key, "merged", body)
                elif user_event == "closed" and action == "closed" and not merged:
                    if "closed" in watch.events:
                        logger.info("PR %s closed (not merged)", watch_key)
                        self.state.feed_event(watch_key, "closed", body)
                # Skip other pull_request actions for merged/closed handlers
                continue

            # For other events, check if watch has a handler
            if user_event in watch.events:
                logger.info(
                    "Event matched: %s -> %s for %s",
                    event_type, user_event, watch_key,
                )
                self.state.feed_event(watch_key, user_event, body)

    def _handle_stop(self):
        self._send_json(200, {"status": "stopping"})
        # Shutdown in background so response is sent
        threading.Thread(target=self._do_shutdown, daemon=True).start()

    def _do_shutdown(self):
        time.sleep(0.5)  # Let response flush
        self.state.shutdown()
        self.server.shutdown()


# ---------------------------------------------------------------------------
# Threaded HTTP server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def __init__(self, port: int, state: PRWatchState):
        self.state = state
        super().__init__(("127.0.0.1", port), WebhookHandler)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="pr-watch server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground with console output")
    args = parser.parse_args()

    setup_logging(foreground=args.foreground)

    state = PRWatchState(port=args.port)
    state.load()

    # Write PID file
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    # Start watchdog thread
    watchdog = threading.Thread(target=state.watchdog_loop, daemon=True)
    watchdog.start()

    # Restart forwarders for existing watches
    state.restart_all_forwarders()

    # Run catch-up for missed events
    catchup = threading.Thread(target=state.run_catchup, daemon=True)
    catchup.start()

    # Start HTTP server
    server = ThreadedHTTPServer(args.port, state)

    def signal_handler(signum, frame):
        logger.info("Signal %s received", signum)
        state.shutdown()
        server.shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("pr-watch server listening on http://127.0.0.1:%d", args.port)
    if args.foreground:
        print(f"pr-watch server listening on http://127.0.0.1:{args.port}")
        print("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.shutdown()

    sys.exit(0)


if __name__ == "__main__":
    main()
