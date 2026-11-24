#!/usr/bin/env python3
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
BRIDGE_URL = os.getenv("CODEX_WATCH_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
MAX_BODY_LENGTH = 16000


def load_token():
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
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if value:
                return value
    raise RuntimeError("CODEX_WATCH_TOKEN not configured")


def post(path, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        BRIDGE_URL + path,
        data=body,
        headers={
            "Authorization": "Bearer " + load_token(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        result = json.loads(response.read().decode("utf-8"))
        if response.status not in (200, 201, 202) or not result.get("ok"):
            raise RuntimeError("Bridge rejected notification")
        return result


def main():
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("Expected one JSON argument")
        notification = json.loads(sys.argv[1])
        kind = notification.get("type")
        thread_id = str(notification.get("thread-id") or "").strip()
        turn_id = str(notification.get("turn-id") or "").strip()
        cwd = str(notification.get("cwd") or "").strip()
        if kind == "approval-requested":
            if thread_id:
                post("/v1/threads/observe", {"thread_id": thread_id, "cwd": cwd})
            return 0
        if kind != "agent-turn-complete" or not thread_id or not turn_id:
            return 0
        message = str(notification.get("last-assistant-message") or "Codex turn completed.").strip()
        post("/v1/completions", {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "cwd": cwd,
            "message": message[:MAX_BODY_LENGTH],
        })
    except (Exception, HTTPError, URLError) as exc:
        print("[Codex Watch] Notification failed: " + str(exc), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
