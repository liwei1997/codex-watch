#!/usr/bin/env python3
"""
Codex CLI -> Codex Watch Bridge

Receives Codex agent-turn-complete notifications and forwards
them to the local Bridge API.
"""

import json
import os
import socket
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
DEBUG_LOG = PROJECT_ROOT / "data" / "notify_debug.jsonl"

BRIDGE_URL = os.getenv(
    "CODEX_WATCH_BASE_URL",
    "http://127.0.0.1:8765"
).rstrip("/")

MACHINE_ID = os.getenv(
    "CODEX_WATCH_MACHINE_ID",
    "ubuntu-main"
)

MAX_BODY_LENGTH = 16000


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


def build_event(notification):
    thread_id = notification.get("thread-id", "")
    turn_id = notification.get("turn-id", "")
    cwd = notification.get("cwd", "")

    assistant_message = (
        notification.get("last-assistant-message") or
        "Codex turn completed."
    ).strip()

    if len(assistant_message) > MAX_BODY_LENGTH:
        assistant_message = (
            assistant_message[:MAX_BODY_LENGTH]
            + "\n\n[Message truncated]"
        )

    project_name = Path(cwd).name if cwd else "Codex"

    # Deterministic ID prevents duplicate events for the same turn.
    if thread_id and turn_id:
        event_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"codex-watch:{thread_id}:{turn_id}"
        ))
    else:
        event_id = str(uuid.uuid4())

    return {
        "event_id": event_id,
        "source": "codex",
        "machine_id": MACHINE_ID,
        "session_id": thread_id or None,
        "type": "task_completed",
        "title": f"Codex · {project_name}",
        "body": assistant_message,
        "payload": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "cwd": cwd,
            "codex_event_type": notification.get("type"),
        },
    }


def log_raw(notification):
    """Capture the raw hook payload; the exact approval-requested fields
    are learned from this log."""
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(notification, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _first(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _payload_dicts(notification):
    """Top level plus one level of common nested containers."""
    yield notification
    for key in ("approval", "item", "request", "data", "payload"):
        nested = notification.get(key)
        if isinstance(nested, dict):
            yield nested


def build_approval_data(notification):
    thread_id = turn_id = cwd = command = reason = kind = None
    item_id = available = None

    for mapping in _payload_dicts(notification):
        thread_id = thread_id or _first(
            mapping, ("thread-id", "threadId", "thread_id"))
        turn_id = turn_id or _first(
            mapping, ("turn-id", "turnId", "turn_id"))
        cwd = cwd or _first(mapping, ("cwd", "working-directory"))
        command = command or _first(
            mapping, ("command", "command-line", "cmd", "shell-command"))
        reason = reason or _first(
            mapping, ("reason", "justification", "description", "message"))
        kind = kind or _first(
            mapping, ("kind", "decision-type", "approval-type", "approval"))
        item_id = item_id or _first(
            mapping, ("item-id", "itemId", "item_id"))
        available = available or _first(
            mapping,
            ("available-decisions", "availableDecisions",
             "available_decisions"))

    # "type" is the notification discriminator; only reuse it as kind if
    # it is not the discriminator itself.
    if not kind:
        maybe_kind = _first(notification, ("kind", "type"))
        if maybe_kind and maybe_kind != notification.get("type"):
            kind = maybe_kind

    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise RuntimeError("approval payload missing thread id")

    return {
        "thread_id": thread_id,
        "turn_id": str(turn_id or "").strip(),
        "kind": str(kind or "command").strip() or "command",
        "command": str(command or "").strip(),
        "cwd": str(cwd or "").strip(),
        "reason": str(reason or "").strip(),
        "available_decisions": available,
        "item_id": item_id,
    }


def send_approval(data):
    token = load_token()

    body = json.dumps(
        data,
        ensure_ascii=False
    ).encode("utf-8")

    request = Request(
        f"{BRIDGE_URL}/v1/approvals",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=3) as response:
        result = json.loads(response.read().decode("utf-8"))

        if response.status not in (200, 201):
            raise RuntimeError(
                f"Unexpected HTTP status: {response.status}"
            )

        if not result.get("ok"):
            raise RuntimeError("Bridge rejected the approval")

        return result


def send_event(event):
    token = load_token()

    data = json.dumps(
        event,
        ensure_ascii=False
    ).encode("utf-8")

    request = Request(
        f"{BRIDGE_URL}/v1/events",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=3) as response:
        result = json.loads(response.read().decode("utf-8"))

        if response.status not in (200, 201):
            raise RuntimeError(
                f"Unexpected HTTP status: {response.status}"
            )

        if not result.get("ok"):
            raise RuntimeError("Bridge rejected the event")

        return result


def main():
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("Expected one JSON argument")

        notification = json.loads(sys.argv[1])

        log_raw(notification)

        notification_type = notification.get("type")

        if notification_type == "approval-requested":
            data = build_approval_data(notification)
            result = send_approval(data)
            print(
                "[Codex Watch] Approval delivered:",
                result.get("approval_id"),
                file=sys.stderr,
            )
            return 0

        if notification_type != "agent-turn-complete":
            return 0

        event = build_event(notification)
        result = send_event(event)

        print(
            "[Codex Watch] Event delivered:",
            result.get("event_id"),
            file=sys.stderr,
        )

    except (Exception, HTTPError, URLError) as exc:
        # Notifications must never interrupt the Codex workflow.
        print(
            f"[Codex Watch] Notification failed: {exc}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
