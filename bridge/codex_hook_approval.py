#!/usr/bin/env python3
"""
Codex PermissionRequest hook -> Codex Watch Bridge.

Reads ONE JSON object from stdin (hook payload), logs it, and forwards a
pending approval to the local Bridge API (POST /v1/approvals).

Design (notify-only): this hook never returns a decision. Codex's normal
approval flow (terminal prompt) stays authoritative; the watch only shows
the request and records a decision for visibility.

Fail-open: any error is logged and the hook exits 0.
Never prints JSON to stdout (stdout would be parsed as a hook decision).
"""

import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
DEBUG_LOG = PROJECT_ROOT / "data" / "hook_debug.jsonl"

BRIDGE_URL = os.getenv(
    "CODEX_WATCH_BASE_URL",
    "http://127.0.0.1:8765"
).rstrip("/")

MAX_CMD_LENGTH = 16000


def load_token():
    """Read the existing Bridge token without exposing it."""

    token = os.getenv("CODEX_WATCH_TOKEN")

    if token:
        return token.strip()

    if not ENV_FILE.exists():
        raise RuntimeError("Bridge .env file not found")

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()

        if line.startswith("export "):
            line = line[7:].strip()

        if line.startswith("CODEX_WATCH_TOKEN="):
            value = line.split("=", 1)[1].strip()

            if len(value) >= 2 and value[0] == value[-1]:
                if value[0] in ("'", '"'):
                    value = value[1:-1]

            if value:
                return value

    raise RuntimeError("CODEX_WATCH_TOKEN not configured")


def debug_log(record):
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def build_command(tool_name, tool_input):
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
        desc = tool_input.get("description")
        if isinstance(desc, str) and desc.strip():
            return "[%s] %s" % (tool_name, desc.strip())
        try:
            return "[%s] %s" % (tool_name, json.dumps(tool_input, ensure_ascii=False))
        except Exception:
            return "[%s] %r" % (tool_name, tool_input)
    if tool_input is None:
        return "[%s] (no input)" % tool_name
    return "[%s] %s" % (tool_name, str(tool_input))


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        debug_log({"ok": False, "stage": "parse", "error": repr(exc),
                   "raw_head": raw[:2000]})
        return 0

    if not isinstance(payload, dict):
        debug_log({"ok": False, "stage": "parse", "error": "not an object",
                   "raw_head": str(payload)[:2000]})
        return 0

    debug_log({"ok": True, "stage": "raw", "payload": payload})

    session_id = str(payload.get("session_id") or "").strip()
    turn_id = str(payload.get("turn_id") or "").strip()
    tool_name = str(payload.get("tool_name") or "unknown").strip()
    tool_input = payload.get("tool_input")
    cwd = str(payload.get("cwd") or "").strip()

    if not session_id:
        debug_log({"ok": False, "stage": "extract", "error": "no session_id"})
        return 0

    command = build_command(tool_name, tool_input)
    if len(command) > MAX_CMD_LENGTH:
        command = command[:MAX_CMD_LENGTH] + "\n[truncated]"

    kind = "mcp" if "." in tool_name else "command"
    reason = "PermissionRequest hook: %s" % tool_name

    try:
        token = load_token()
    except Exception as exc:
        debug_log({"ok": False, "stage": "token", "error": repr(exc)})
        return 0

    body = json.dumps({
        "thread_id": session_id,
        "turn_id": turn_id,
        "kind": kind,
        "command": command,
        "cwd": cwd,
        "reason": reason,
        "transcript_path": payload.get("transcript_path"),
    }).encode("utf-8")

    req = Request(
        BRIDGE_URL + "/v1/approvals",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=5) as resp:
            resp_body = resp.read().decode("utf-8", "replace")
            debug_log({"ok": True, "stage": "post",
                       "status": resp.status, "resp": resp_body[:2000]})
    except (HTTPError, URLError, OSError) as exc:
        debug_log({"ok": False, "stage": "post", "error": repr(exc)})

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
