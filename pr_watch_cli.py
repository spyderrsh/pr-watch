#!/usr/bin/env python3
"""pr-watch CLI: Manage the pr-watch webhook listener service."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_PORT = 8765
STATE_DIR = Path.home() / ".claude"
PID_FILE = STATE_DIR / "pr-watch-server.pid"
SERVER_SCRIPT = Path(__file__).parent / "server.py"

VALID_EVENTS = ["review_comment", "review", "checks", "merged", "closed"]


# ---------------------------------------------------------------------------
# Server communication
# ---------------------------------------------------------------------------

def server_request(method: str, path: str, body: dict | None = None, port: int = DEFAULT_PORT, timeout: int = 5) -> dict | None:
    url = f"http://localhost:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (URLError, TimeoutError):
        return None


def is_server_running(port: int = DEFAULT_PORT) -> bool:
    result = server_request("GET", "/health", port=port)
    return result is not None and result.get("status") == "ok"


def ensure_server_running(port: int = DEFAULT_PORT) -> bool:
    """Start server if not running. Returns True if server is available."""
    if is_server_running(port):
        return True

    print(f"Server not running. Starting on port {port}...")
    return start_server_background(port)


def start_server_background(port: int = DEFAULT_PORT) -> bool:
    """Start server as a detached background process."""
    # Check gh-webhook extension
    try:
        result = subprocess.run(
            ["gh", "extension", "list"], capture_output=True, text=True, timeout=10
        )
        if "cli/gh-webhook" not in result.stdout:
            print("Error: gh webhook extension not installed.")
            print("Run: gh extension install cli/gh-webhook")
            return False
    except Exception as e:
        print(f"Error checking gh extensions: {e}")
        return False

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

    try:
        subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT), "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=True,
        )
    except Exception as e:
        print(f"Failed to start server: {e}")
        return False

    # Wait for server to be ready
    for _ in range(20):
        time.sleep(0.5)
        if is_server_running(port):
            print(f"Server started on port {port}")
            return True

    print("Server failed to start within 10 seconds. Check ~/.claude/pr-watch.log")
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_start(args):
    port = args.port

    if is_server_running(port):
        print(f"Server already running on port {port}")
        return

    if args.foreground:
        print(f"Starting server in foreground on port {port}...")
        os.execvp(sys.executable, [sys.executable, str(SERVER_SCRIPT), "--port", str(port), "--foreground"])
    else:
        if start_server_background(port):
            print("Server is ready.")
        else:
            sys.exit(1)


def cmd_register(args):
    port = args.port

    if not ensure_server_running(port):
        sys.exit(1)

    # Parse --on event names
    if not args.on:
        print("Error: at least one --on EVENT is required")
        sys.exit(1)

    events = []
    for event_name in args.on:
        if event_name not in VALID_EVENTS:
            print(f"Error: unknown event '{event_name}'. Valid: {', '.join(VALID_EVENTS)}")
            sys.exit(1)
        events.append(event_name)

    # Resolve cwd
    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        print(f"Error: cwd is not a directory: {cwd}")
        sys.exit(1)

    # Check command files exist locally before hitting the server
    commands_dir = cwd / ".claude" / "commands"
    for evt in events:
        cmd_file = commands_dir / f"{evt}.md"
        if not cmd_file.is_file():
            print(f"Error: command file not found: {cmd_file}")
            print(f"  Create it at: <cwd>/.claude/commands/{evt}.md")
            sys.exit(1)

    body = {
        "repo": args.repo,
        "pr": args.pr,
        "cwd": str(cwd).replace("\\", "/"),
        "cli": args.cli,
        "events": events,
    }

    result = server_request("POST", "/register", body, port=port, timeout=30)
    if result is None:
        print("Error: could not reach server")
        sys.exit(1)

    if "errors" in result:
        print("Registration errors:")
        for err in result["errors"]:
            print(f"  - {err}")
        sys.exit(1)

    status = result.get("status", "")
    if status == "no_events":
        print(f"No events registered: {result.get('message', '')}")
    else:
        registered = result.get("events", [])
        if registered:
            print(f"Registered watch: {result.get('key')}")
            print(f"  Events: {', '.join(registered)}")
            print(f"  CLI: {args.cli}")
            print(f"  CWD: {cwd}")

    if result.get("fired_immediately"):
        print(f"  Fired immediately: {', '.join(result['fired_immediately'])}")

    if result.get("rejected"):
        print(f"  Rejected:")
        for r in result["rejected"]:
            print(f"    - {r}")


def cmd_list(args):
    port = args.port

    if not is_server_running(port):
        print("Server is not running.")
        return

    result = server_request("GET", "/watches", port=port)
    if result is None:
        print("Error: could not reach server")
        sys.exit(1)

    watches = result.get("watches", [])
    if not watches:
        print("No active watches.")
        return

    for w in watches:
        key = w.get("key", "?")
        cli = w.get("cli", "?")
        cwd = w.get("cwd", "?")
        events = w.get("events", [])
        sessions = w.get("sessions", {})
        pending = w.get("pending_debounce", {})

        print(f"\n  {key}")
        print(f"    CLI: {cli}  |  CWD: {cwd}")
        print(f"    Events:")
        for evt in events:
            session = sessions.get(evt)
            session_str = f" (session: {session[:8]}...)" if session and session != "started" else ""
            if session == "started":
                session_str = " (session active)"
            pend = pending.get(evt)
            pend_str = ""
            if pend:
                pend_str = f" [debouncing: {pend['payload_count']} events, fires at {pend['fires_at']}]"
            cmd_path = f".claude/commands/{evt}.md"
            print(f"      {evt} (/{evt}){session_str}{pend_str}")

    print()


def cmd_unregister(args):
    port = args.port

    if not is_server_running(port):
        print("Server is not running.")
        return

    result = server_request("DELETE", f"/watch?repo={args.repo}&pr={args.pr}", port=port)
    if result is None:
        print("Error: could not reach server")
        sys.exit(1)

    if result.get("status") == "not_found":
        print(f"No watch found for {args.repo}#{args.pr}")
    else:
        print(f"Unregistered: {result.get('key')}")


def cmd_status(args):
    port = args.port

    if not is_server_running(port):
        print("Server is not running.")

        # Check PID file for stale state
        if PID_FILE.exists():
            pid = PID_FILE.read_text().strip()
            print(f"  Stale PID file found (PID {pid})")

        return

    result = server_request("GET", "/health", port=port)
    if result is None:
        print("Error: could not reach server")
        sys.exit(1)

    print(f"Server: running on port {result.get('port')}")
    print(f"  Started: {result.get('started_at')}")
    print(f"  Watches: {result.get('watch_count')}")

    forwarders = result.get("forwarders", {})
    if forwarders:
        print(f"  Forwarders:")
        for repo, info in forwarders.items():
            status = "alive" if info.get("alive") else "DEAD"
            retries = info.get("retry_count", 0)
            events = ", ".join(info.get("events", []))
            print(f"    {repo}: {status} (retries: {retries})")
            print(f"      events: {events}")
    else:
        print(f"  Forwarders: none")


def cmd_stop(args):
    port = args.port

    if not is_server_running(port):
        print("Server is not running.")
        return

    result = server_request("POST", "/stop", port=port)
    if result:
        print("Server stopping...")
    else:
        print("Error: could not reach server")
        sys.exit(1)

    # Wait for shutdown
    for _ in range(10):
        time.sleep(1)
        if not is_server_running(port):
            print("Server stopped.")
            return

    print("Server may still be shutting down. Check status with: pr-watch status")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="pr-watch",
        description="Manage the pr-watch GitHub webhook listener service",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = sub.add_parser("start", help="Start the pr-watch server")
    p_start.add_argument("--foreground", "-f", action="store_true", help="Run in foreground")
    p_start.set_defaults(func=cmd_start)

    # register
    p_reg = sub.add_parser("register", help="Register a PR watch")
    p_reg.add_argument("--repo", required=True, help="Repository (owner/repo)")
    p_reg.add_argument("--pr", type=int, required=True, help="PR number")
    p_reg.add_argument("--cwd", default=os.getcwd(), help="Working directory for CLI sessions (default: cwd)")
    p_reg.add_argument(
        "--on", action="append",
        metavar="EVENT",
        help=f"Event to watch (uses <cwd>/.claude/commands/<EVENT>.md). Events: {', '.join(VALID_EVENTS)}",
    )
    p_reg.add_argument("--cli", choices=["claude", "opencode"], default="claude", help="CLI tool (default: claude)")
    p_reg.set_defaults(func=cmd_register)

    # list
    p_list = sub.add_parser("list", help="List active watches")
    p_list.set_defaults(func=cmd_list)

    # unregister
    p_unreg = sub.add_parser("unregister", help="Remove a PR watch")
    p_unreg.add_argument("--repo", required=True, help="Repository (owner/repo)")
    p_unreg.add_argument("--pr", type=int, required=True, help="PR number")
    p_unreg.set_defaults(func=cmd_unregister)

    # status
    p_status = sub.add_parser("status", help="Show server status")
    p_status.set_defaults(func=cmd_status)

    # stop
    p_stop = sub.add_parser("stop", help="Stop the pr-watch server")
    p_stop.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
